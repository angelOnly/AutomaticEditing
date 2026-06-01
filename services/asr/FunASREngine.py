# -*- coding: utf-8 -*-
"""
FunASREngine - FunASR 语音转文字引擎封装
支持 SenseVoice-Small 模型，GPU/CPU 双模式
"""

import os
import time
import torch
from loguru import logger

class FunASREngine:
    """FunASR 引擎封装，支持动态 GPU/CPU 切换"""

    def __init__(self, model_name="iic/SenseVoiceSmall",
                 vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                 punc_model="iic/punc_ct-transformer_cn-en-common-vocab471067-large",
                 device="cuda:0"):
                 
        # 将相对模型路径转换为绝对路径，避免由于工作目录不同导致找不到本地模型
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        def _resolve_path(m_path):
            if m_path and not m_path.startswith("/") and not m_path[1:3] == ":\\":
                # 认为是相对路径
                abs_p = os.path.join(base_dir, "models", m_path)
                if os.path.exists(abs_p):
                    return abs_p
            return m_path
            
        self.model_name = _resolve_path(model_name)
        self.vad_model_name = _resolve_path(vad_model)
        self.punc_model_name = _resolve_path(punc_model)
        
        self.default_device = device
        self.current_device = None

        # 模型实例（延迟加载）
        self._model = None
        self._is_loaded = False

        logger.info(f"FunASREngine initialized. Model: {model_name}, Default device: {device}")

    def _load_model(self, device=None):
        """加载或重新加载模型到指定设备"""
        target_device = device or self.default_device

        # 如果已经加载在相同设备上，跳过
        if self._is_loaded and self.current_device == target_device:
            return

        # 如果设备切换，先卸载旧模型
        if self._is_loaded and self.current_device != target_device:
            self._unload_model()

        logger.info(f"Loading FunASR model on {target_device}...")
        start_time = time.time()

        try:
            from funasr import AutoModel

            self._model = AutoModel(
                model=self.model_name,
                vad_model=self.vad_model_name,
                punc_model=self.punc_model_name,
                device=target_device,
                trust_remote_code=True,
            )
            self.current_device = target_device
            self._is_loaded = True

            elapsed = time.time() - start_time
            logger.info(f"FunASR model loaded on {target_device} in {elapsed:.1f}s")
        except Exception as e:
            logger.error(f"Failed to load FunASR model: {e}")
            raise

    def _unload_model(self):
        """卸载模型，释放显存/内存"""
        if self._model is not None:
            logger.info(f"Unloading FunASR model from {self.current_device}...")
            del self._model
            self._model = None
            self._is_loaded = False

            if self.current_device and 'cuda' in str(self.current_device):
                torch.cuda.empty_cache()
                logger.info("CUDA cache cleared.")

            self.current_device = None

    def transcribe(self, audio_path, device=None, language="auto", batch_size_s=300):
        """
        转录音频文件
        """
        result = {
            'success': False,
            'text': '',
            'segments': [],
            'duration': 0,
            'device': '',
            'elapsed': 0,
            'error': None,
        }

        # 验证文件
        if not os.path.exists(audio_path):
            result['error'] = f"Audio file not found: {audio_path}"
            logger.error(result['error'])
            return result

        file_size_mb = os.path.getsize(audio_path) / 1024 / 1024
        logger.info(f"Starting transcription: {audio_path} ({file_size_mb:.1f} MB)")

        try:
            # 加载模型
            self._load_model(device)
            result['device'] = self.current_device

            start_time = time.time()

            # 执行转录
            res = self._model.generate(
                input=audio_path,
                batch_size_s=batch_size_s,
                language=language,
            )

            elapsed = time.time() - start_time
            result['elapsed'] = round(elapsed, 2)

            # 解析结果
            if res and len(res) > 0:
                # SenseVoice 返回格式：[{"key": ..., "text": ..., ...}]
                full_text = ""
                segments = []

                for item in res:
                    text = item.get('text', '')
                    full_text += text + " "

                    # 尝试提取时间戳段
                    if 'timestamp' in item and item['timestamp']:
                        timestamps = item['timestamp']
                        sentence = item.get('text', '')
                        if isinstance(timestamps, list) and len(timestamps) >= 2:
                            segments.append({
                                'start': timestamps[0][0] / 1000.0 if isinstance(timestamps[0], list) else timestamps[0] / 1000.0,
                                'end': timestamps[-1][-1] / 1000.0 if isinstance(timestamps[-1], list) else timestamps[-1] / 1000.0,
                                'text': sentence,
                            })
                    else:
                        # 无时间戳信息，整段作为一个 segment
                        segments.append({
                            'start': 0,
                            'end': 0,
                            'text': text,
                        })

                result.update({
                    'success': True,
                    'text': full_text.strip(),
                    'segments': segments,
                })

            else:
                result['error'] = "FunASR returned empty result"
                logger.warning(result['error'])

        except Exception as e:
            result['error'] = f"Transcription failed: {str(e)}"
            logger.error(f"Transcription error: {e}")

        return result

    def get_status(self):
        """获取引擎状态"""
        return {
            'model': self.model_name,
            'is_loaded': self._is_loaded,
            'current_device': self.current_device,
            'default_device': self.default_device,
        }

    def switch_device(self, device):
        if self.current_device == device:
            return
        logger.info(f"Switching device: {self.current_device} → {device}")
        self._unload_model()
        self.default_device = device

    def release(self):
        """释放所有资源"""
        self._unload_model()
        logger.info("FunASREngine resources released.")
