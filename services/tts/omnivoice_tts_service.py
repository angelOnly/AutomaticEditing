import os
import sys
import time
import subprocess
import torch
import torchaudio
import json
from loguru import logger
from app.config import config
from app.services.voice import new_sub_maker, add_subtitle_event
from edge_tts.submaker import SubMaker

class OmniVoiceService:
    _model = None
    _asr_engine = None

    @classmethod
    def load_model(cls, model_path: str):
        if cls._model is None:
            try:
                from omnivoice import OmniVoice
            except ImportError:
                logger.error("OmniVoice not found. Ensure it is installed in your environment.")
                raise Exception("OmniVoice not found. Ensure it is installed in your environment.")

            logger.info("正在加载 OmniVoice 模型...")
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if torch.cuda.is_available() else torch.float32 
            logger.info(f"计算设备: {device}, 数据类型: {dtype}, 模型路径: {model_path}")
            cls._model = OmniVoice.from_pretrained(
                model_path,
                device_map=device,
                dtype=dtype
            )
            logger.info("OmniVoice 模型加载完成")
        return cls._model

    @classmethod
    def load_asr_engine(cls, asr_model_base: str):
        if cls._asr_engine is None:
            try:
                from app.services.asr.FunASREngine import FunASREngine
            except ImportError:
                logger.error("FunASREngine not found.")
                raise Exception("FunASREngine not found.")

            logger.info("正在初始化 ASR 引擎 (SenseVoice)...")
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            cls._asr_engine = FunASREngine(
                model_name=os.path.join(asr_model_base, "SenseVoiceSmall"),
                vad_model=os.path.join(asr_model_base, "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
                punc_model=os.path.join(asr_model_base, "punc_ct-transformer_cn-en-common-vocab471067-large"),
                device=device
            )
            logger.info("ASR 引擎初始化完成")
        return cls._asr_engine

    @classmethod
    def release(cls):
        if cls._asr_engine:
            cls._asr_engine.release()
            cls._asr_engine = None
        if cls._model:
            del cls._model
            cls._model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

def omnivoice_tts_generate(text: str, voice_file: str, speed: float = 1.0, subtitle_file: str = "") -> SubMaker:
    """
    生成短视频语音和字幕。
    对于 OmniVoice，通过读取配置传入参考音频和参考文本。
    """
    try:
        model_path = config.omnivoice.get("model_path", r"E:\ai\skills\omnivoice\k2-fsa")
        asr_model_base = config.omnivoice.get("asr_model_base", r"E:\ai\skills\RSSNewsLocal\models\iic")
        ref_audio = config.omnivoice.get("reference_audio", "")
        ref_text = config.omnivoice.get("reference_text", "")

        if not ref_audio or not os.path.exists(ref_audio):
            raise Exception("缺少参考音频，或音频文件不存在。请在UI中配置。")

        # 加载模型
        model = OmniVoiceService.load_model(model_path)
        asr_engine = OmniVoiceService.load_asr_engine(asr_model_base)

        logger.info(f"-> 正在进行声音克隆... 参考音频: {ref_audio}")
        t1 = time.time()
        
        # 为了兼容语速调节，如果 OmniVoice 有 speed 参数的话，目前暂且按照默认。
        # 如果需要调整语速，可以在后续音频处理或这里的特定参数调整。
        audio = model.generate(
            text=text,
            ref_audio=ref_audio,
            ref_text=ref_text if ref_text else None
        )

        logger.info(f"保存音频结果 -> {voice_file}")
        # 使用 soundfile 代替 torchaudio 以规避 Windows 下 libtorchcodec 缺失或不兼容的问题
        import soundfile as sf
        audio_numpy = audio[0].cpu().numpy()
        # soundfile 期望 (samples, channels)，而 torch 通常是 (channels, samples)
        if audio_numpy.ndim > 1:
            audio_numpy = audio_numpy.T
        sf.write(voice_file, audio_numpy, 24000)

        safe_speed = max(0.5, min(2.0, float(speed or 1.0)))
        if abs(safe_speed - 1.0) > 0.001:
            tempo_file = f"{voice_file}.tempo.mp3"
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                voice_file,
                "-filter:a",
                f"atempo={safe_speed}",
                tempo_file,
            ]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                os.replace(tempo_file, voice_file)
                logger.info(f"OmniVoice 闊抽璇€熷凡璋冩暣涓? {safe_speed:.2f}x")
            except Exception as e:
                logger.warning(f"OmniVoice 闊抽璇€熻皟鏁村け璐ワ紝灏嗕娇鐢ㄥ師濮嬭閫? {e}")
                if os.path.exists(tempo_file):
                    os.remove(tempo_file)

        sub_maker = new_sub_maker()
        
        logger.info(f"-> 正在生成配套字幕事件...")
        asr_result = asr_engine.transcribe(voice_file)

        if asr_result.get('success') and asr_result.get('segments'):
            for seg in asr_result['segments']:
                start_offset = int(seg['start'] * 10000000)
                end_offset = int(seg['end'] * 10000000)
                import re
                seg_text = re.sub(r'<\s*\|\s*.*?\s*\|\s*>', '', seg['text']).strip()
                add_subtitle_event(
                    sub_maker,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    text=seg_text,
                    boundary_type="WordBoundary"
                )
        else:
            logger.error(f"ASR生成错误: {asr_result.get('error', 'Unknown Error')}")
            
        t2 = time.time()
        logger.success(f"OmniVoice 克隆及字幕生成完成，总耗时: {t2 - t1:.2f} 秒")

        return sub_maker

    except Exception as e:
        logger.exception(f"omnivoice tts 生成失败: {str(e)}")
        raise
