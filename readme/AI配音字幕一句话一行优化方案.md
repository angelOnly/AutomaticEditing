# AI 配音字幕“一句话一行”显示优化方案

## 1. 现象与目标

从截图看，当前 AI 配音字幕在画面底部显示为多行堆叠，且每行是由播放器 / FFmpeg 字幕渲染器自动换行出来的，不是按语义句子拆分。用户看到的是类似：

> 近期美伊双方处于边谈边打的特殊状态，  
> 伊朗革命卫队公开表态，若最终达成的停  
> 火协议遭到违反，伊朗将立刻做出对等回应。

这种显示方式的问题是：

1. 一条字幕过长，渲染器按宽度硬折行，导致一句话被截成几段，阅读感差。
2. 同一时间出现 2-3 行字幕，占用画面下方空间，尤其横屏新闻人物口播画面，会挡住人物、桌面和画面字幕。
3. 字幕切换节奏和 AI 配音句子节奏不一致，用户感觉像“字幕糊在一起”。
4. 当前字幕没有“每句独立显示”的规则，SRT 中一个 block 可能包含一整段解说，而不是一句话。

优化目标：

- AI 配音字幕默认做到“一句话一条字幕，一句话一行”。
- 每条字幕尽量不超过一行宽度；如果一句话太长，则按逗号、分号、顿号等自然停顿拆成更短的 clause。
- 每次屏幕只显示 1 行字幕，最多允许 2 行兜底，不再出现 3 行堆叠。
- 字幕时间必须跟随实际 TTS 分段时间，避免字幕快于或慢于 AI 配音。
- 代码层面要可配置，后续可以调字号、底部距离、每行最大字数、最短字幕时长等。

---

## 2. 当前代码定位

我检查了上传代码包，字幕相关逻辑主要集中在：

### 2.1 `newsclip_agent/pipeline.py`

#### `step_subtitles`

位置大约在 `newsclip_agent/pipeline.py` 第 1787-1812 行。

当前逻辑：

```python
if self.duration_settings.subtitle_follow_actual_tts and tts_segments:
    lines = self._tts_segments_to_srt(tts_segments)
elif isinstance(item.get("narration_segments"), list) and item.get("narration_segments"):
    lines = self._segments_to_srt(item["narration_segments"])
else:
    lines = self._script_to_srt(text, total_duration=duration if duration > 0 else None)
```

这里的设计是：优先用 TTS segments 生成字幕，其次用 narration_segments，最后用整段文案生成字幕。

问题在于：

- `_tts_segments_to_srt()` 只是把每个 TTS segment 原样写成一个 SRT block。
- 如果 TTS segment 的 text 很长，SRT 里就是一整段长文本。
- FFmpeg 的 `subtitles` 滤镜会根据画面宽度自动折行，所以最后看起来就是 2-3 行堆叠。

#### `_script_to_srt`

位置大约在 `pipeline.py` 第 2541-2557 行。

当前逻辑会按 `。！？!?` 拆句：

```python
parts = [p.strip() for p in re.split(r"[。！？!?]\s*", clean) if p.strip()]
```

但它存在两个问题：

1. 只在完全没有 TTS segments / narration_segments 时才使用，正常 AI 配音流程基本不会走到这里。
2. 拆句后没有处理“单句过长”的情况，例如一句 35-50 个汉字，仍然会被 FFmpeg 自动折成多行。

#### `_segments_to_srt` / `_tts_segments_to_srt`

位置大约在 `pipeline.py` 第 2559-2593 行。

当前逻辑：

```python
blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
```

也就是：一个 segment 直接生成一个字幕 block，不做二次拆句、不做字数限制、不做标点保留。

#### burn-in 样式

位置大约在 `pipeline.py` 第 2506-2510 行。

当前样式：

```python
style = "Fontsize=22,PrimaryColour=&H00000000,OutlineColour=&H00FFFFFF,BorderStyle=1,Outline=3,Shadow=0,Blur=1"
```

问题：

- 没有设置 `Alignment`，不同环境下默认位置可能不稳定。
- 没有设置 `MarginV`，字幕底部距离不好控制。
- 没有设置 `WrapStyle`，长句还是依赖自动换行。
- `Fontsize=22` 对 1920 横屏还可以，但如果要一行显示，应该配合“每行最大字数”控制，而不是只调字号。

### 2.2 `newsclip_agent/prompts.py`

#### `VOICEOVER_PROMPT`

位置大约在 `prompts.py` 第 336-365 行。

当前提示词只要求：

```text
最后把所有 narration_segments 串联为 narration_text，TTS 和字幕都将使用 narration_text。
```

问题：

- 没有要求模型给出适合字幕展示的短句。
- 没有要求每个 `narration_segments[].text` 尽量按完整短句组织。
- 没有要求避免一个 segment 内塞入多个长句。

#### `VOICEOVER_LIGHT_PROMPT`

位置大约在 `prompts.py` 第 635-646 行。

同样只要求 narration_segments 对应 timing contracts，没有字幕可读性约束。

---

## 3. 根因分析

当前问题不是播放器预览的问题，也不是单纯字号太大的问题，核心原因是“字幕生成粒度太粗”。

### 3.1 TTS segment 与字幕 sentence 混用了同一层级

现在流程大概是：

```text
voiceover_script.narration_segments
        ↓
tts.outputs[].segments
        ↓
subtitle.srt
        ↓
ffmpeg subtitles burn-in
```

`narration_segments` 的本意更接近“镜头解说段”，不是“屏幕字幕句子”。一个镜头 4-8 秒，里面可能有 1-3 句话。当前代码把“镜头解说段”直接当成“字幕条目”，所以字幕自然会太长。

### 3.2 SRT 文本没有二次拆分

即使模型输出的是：

```text
近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。
```

当前 `_tts_segments_to_srt()` 会直接写成：

```srt
1
00:00:00,000 --> 00:00:06,000
近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。
```

FFmpeg 渲染时只能按画面宽度硬折行，结果就是一句话被截断成 2-3 行。

### 3.3 字幕没有最大字数策略

中文横屏新闻字幕建议：

- 16:9 横屏：每行 18-24 个汉字比较稳。
- 9:16 竖屏：每行 10-16 个汉字比较稳。
- 如果一条字幕超过这个范围，就应该主动拆成多个连续字幕 cue，而不是让渲染器自动换行。

当前代码没有这个配置。

### 3.4 提示词没有约束字幕友好输出

模型生成 `narration_segments[].text` 时，通常会为了叙事完整，把一个镜头的全部说明写成一整段。代码层面如果没有二次拆分，字幕就会过长。

---

## 4. 推荐优化方案总览

建议分三层优化：

### 第一层：代码层强制修复，必须做

新增一个统一的字幕拆分器：

```text
原始 segment text
  → 按句号、问号、感叹号拆完整句
  → 单句过长时按逗号、分号、顿号、冒号拆 clause
  → clause 仍过长时按最大字数硬切
  → 按字数权重分配每条字幕开始 / 结束时间
  → 生成 SRT，每个 cue 只放一行字幕
```

这是最重要的改动。即使大模型输出不好，也能保证字幕显示不会乱。

### 第二层：字幕样式配置化，建议做

新增 `[subtitle]` 配置：

```toml
[subtitle]
enabled = true
mode = "sentence"                 # segment / sentence
max_chars_per_line_16_9 = 22
max_chars_per_line_9_16 = 14
max_lines = 1
min_cue_seconds = 1.0
max_cue_seconds = 4.0
prefer_clause_split = true
font_size_16_9 = 24
font_size_9_16 = 20
margin_v_16_9 = 70
margin_v_9_16 = 120
outline = 3
shadow = 0
primary_colour = "&H00000000"
outline_colour = "&H00FFFFFF"
```

后续可以直接调配置，不用改代码。

### 第三层：提示词约束，建议做

让模型在 `narration_segments[].text` 中尽量输出字幕友好的短句：

- 每个 text 尽量由 1-2 个短句组成。
- 单句尽量不超过 22 个汉字。
- 如果一句过长，主动拆成逗号短句。
- 不要输出过长复合句。

但要注意：提示词只能降低问题概率，不能作为最终保障。最终必须靠代码强制拆分。

---

## 5. 具体代码修改方案

下面给的是可以直接按文件修改的开发指导。

---

## 5.1 修改 `config.toml`：新增字幕配置

在 `config.toml` 的 `[voiceover]` 后面，或者 `[reassembly]` 前面新增：

```toml
[subtitle]
# 字幕生成模式：
# segment：保持旧逻辑，一个 narration/TTS segment 一个字幕块
# sentence：推荐，新逻辑，一句话/短从句一个字幕块
mode = "sentence"

# 横屏 16:9 推荐每行最大汉字数。超过后会主动按标点拆分。
max_chars_per_line_16_9 = 22

# 竖屏 9:16 推荐更短，避免字幕超宽。
max_chars_per_line_9_16 = 14

# 默认只允许一行。真的无法避免时，也不建议超过 2。
max_lines = 1

# 单条字幕最短展示时间，太短会闪。
min_cue_seconds = 1.0

# 单条字幕最长展示时间，太长会显得字幕不动。
max_cue_seconds = 4.0

# 是否优先按逗号、分号、顿号继续拆分长句。
prefer_clause_split = true

# burn-in 样式
font_size_16_9 = 24
font_size_9_16 = 20
margin_v_16_9 = 70
margin_v_9_16 = 120
outline = 3
shadow = 0
primary_colour = "&H00000000"
outline_colour = "&H00FFFFFF"
```

如果你现在只做横屏，可以先只关心：

```toml
[subtitle]
mode = "sentence"
max_chars_per_line_16_9 = 22
min_cue_seconds = 1.0
max_cue_seconds = 4.0
font_size_16_9 = 24
margin_v_16_9 = 70
```

---

## 5.2 修改 `newsclip_agent/config.py`：增加 `subtitle` 配置访问器

在 `ProjectConfig` 类里，`voiceover` 属性后面增加：

```python
    @property
    def subtitle(self) -> dict[str, Any]:
        return self.raw.get("subtitle", {})
```

修改后大概是：

```python
    @property
    def voiceover(self) -> dict[str, Any]:
        return self.raw.get("voiceover", {})

    @property
    def subtitle(self) -> dict[str, Any]:
        return self.raw.get("subtitle", {})
```

---

## 5.3 修改 `newsclip_agent/pipeline.py`：增加字幕配置读取方法

在 `PipelineRunner` 类中新增几个 helper，建议放在 `_video_filter()` 附近，或者字幕函数 `_script_to_srt()` 前面。

新增代码：

```python
    def _subtitle_config(self) -> dict[str, Any]:
        return self.config.raw.get("subtitle", {}) or {}

    def _subtitle_max_chars_per_line(self) -> int:
        cfg = self._subtitle_config()
        if self.options.aspect_ratio == "9:16":
            return int(cfg.get("max_chars_per_line_9_16", 14))
        return int(cfg.get("max_chars_per_line_16_9", 22))

    def _subtitle_min_cue_seconds(self) -> float:
        return float(self._subtitle_config().get("min_cue_seconds", 1.0))

    def _subtitle_max_cue_seconds(self) -> float:
        return float(self._subtitle_config().get("max_cue_seconds", 4.0))

    def _subtitle_mode(self) -> str:
        return str(self._subtitle_config().get("mode", "sentence")).strip().lower()
```

说明：

- `aspect_ratio` 已经在 `RunOptions` 里存在，渲染时也在用，所以可以直接复用。
- 默认 mode 用 `sentence`，这样新逻辑默认生效。
- 旧项目如果想回退，只要配置 `mode = "segment"`。

---

## 5.4 修改 `pipeline.py`：新增字幕文本拆分函数

在 `_script_to_srt()` 前面新增：

```python
    def _split_subtitle_text(self, text: str, max_chars: int | None = None) -> list[str]:
        """把一段 AI 配音文案拆成适合屏幕显示的字幕短句。

        目标：
        1. 优先保持完整句子。
        2. 单句太长时按逗号、分号、顿号、冒号拆成短从句。
        3. 仍然太长时按 max_chars 硬切。
        4. 返回的每一项尽量是一行字幕。
        """
        value = clean_voiceover_text(text)
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"【停顿[0-9.]+秒】", "", value)
        if not value:
            return []

        max_chars = max_chars or self._subtitle_max_chars_per_line()

        # 先按完整句拆，保留句末标点。
        sentence_parts = re.findall(r"[^。！？!?]+[。！？!?]?", value)
        sentence_parts = [p.strip() for p in sentence_parts if p.strip()]

        chunks: list[str] = []
        for sentence in sentence_parts:
            if len(sentence) <= max_chars:
                chunks.append(sentence)
                continue

            # 长句继续按自然停顿拆，保留分隔符。
            clause_parts = re.findall(r"[^，,；;、：:]+[，,；;、：:]?", sentence)
            clause_parts = [p.strip() for p in clause_parts if p.strip()]

            buffer = ""
            for part in clause_parts:
                if not buffer:
                    buffer = part
                    continue
                if len(buffer) + len(part) <= max_chars:
                    buffer += part
                else:
                    chunks.extend(self._hard_split_subtitle_chunk(buffer, max_chars))
                    buffer = part
            if buffer:
                chunks.extend(self._hard_split_subtitle_chunk(buffer, max_chars))

        return [c for c in chunks if c.strip()]

    def _hard_split_subtitle_chunk(self, text: str, max_chars: int) -> list[str]:
        value = (text or "").strip()
        if not value:
            return []
        if len(value) <= max_chars:
            return [value]
        return [value[i:i + max_chars] for i in range(0, len(value), max_chars)]
```

为什么这么做：

- `re.findall(r"[^。！？!?]+[。！？!?]?", value)` 会保留句号、问号、感叹号，字幕看起来更自然。
- 长句优先按 `，,；;、：:` 拆，不会粗暴截断新闻语义。
- 兜底硬切可以避免极端长句继续撑满屏幕。

---

## 5.5 修改 `pipeline.py`：新增“按时间分配字幕 cue”的函数

继续在字幕函数区域新增：

```python
    def _subtitle_chunks_to_srt_blocks(
        self,
        *,
        chunks: list[str],
        start: float,
        end: float,
        index_start: int,
    ) -> tuple[list[str], int]:
        """把已经拆好的字幕短句分配到 start-end 时间段内。"""
        if not chunks or end <= start:
            return [], index_start

        total_duration = max(0.1, end - start)
        weights = [max(1, len(c)) for c in chunks]
        weight_total = max(1, sum(weights))
        min_cue = self._subtitle_min_cue_seconds()
        max_cue = self._subtitle_max_cue_seconds()

        blocks: list[str] = []
        cursor = start
        index = index_start

        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                cue_end = end
            else:
                raw_duration = total_duration * weights[i] / weight_total
                cue_duration = min(max(raw_duration, min_cue), max_cue)
                remaining_chunks = len(chunks) - i - 1
                latest_end = end - remaining_chunks * min_cue
                cue_end = min(cursor + cue_duration, latest_end)
                if cue_end <= cursor:
                    cue_end = cursor + max(0.3, raw_duration)

            cue_end = min(cue_end, end)
            if cue_end <= cursor:
                break

            blocks.append(
                f"{index}\n{srt_time(cursor)} --> {srt_time(cue_end)}\n{chunk}\n"
            )
            index += 1
            cursor = cue_end

        return blocks, index
```

作用：

- 一个原始 TTS segment 可能 6 秒，拆成 3 条字幕后，每条字幕自动分配 2 秒左右。
- 按字数权重分配时长，长句显示久一点，短句显示短一点。
- `min_cue_seconds` 防止字幕闪烁。
- `max_cue_seconds` 防止一条字幕停留太久。

---

## 5.6 修改 `_script_to_srt()`

把原来的 `_script_to_srt()` 替换成下面版本：

```python
    def _script_to_srt(self, text: str, total_duration: float | None = None) -> str:
        chunks = self._split_subtitle_text(text)
        if not chunks:
            return ""

        if total_duration and total_duration > 0:
            start = 0.0
            end = float(total_duration)
        else:
            total_chars = sum(max(1, len(c)) for c in chunks)
            end = max(2.0, total_chars / max(self.duration_settings.chars_per_second, 0.1))
            start = 0.0

        blocks, _ = self._subtitle_chunks_to_srt_blocks(
            chunks=chunks,
            start=start,
            end=end,
            index_start=1,
        )
        return "\n".join(blocks)
```

替换原因：

- 不再只按句号粗拆。
- 单句过长时也能继续拆。
- 生成的 SRT 每个 block 只有一行字幕。

---

## 5.7 修改 `_segments_to_srt()`

把原来的 `_segments_to_srt()` 替换成：

```python
    def _segments_to_srt(self, segments: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        index = 1
        for segment in segments:
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = timecode_to_seconds(segment.get("target_start"))
            end = timecode_to_seconds(segment.get("target_end"))
            duration = float(segment.get("target_duration_seconds") or 0)
            if end <= start and duration > 0:
                end = start + duration
            if end <= start:
                continue

            # 兼容旧模式：需要回退时，一个 segment 一个字幕块。
            if self._subtitle_mode() == "segment":
                blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
                index += 1
                continue

            chunks = self._split_subtitle_text(text)
            new_blocks, index = self._subtitle_chunks_to_srt_blocks(
                chunks=chunks,
                start=start,
                end=end,
                index_start=index,
            )
            blocks.extend(new_blocks)

        return "\n".join(blocks)
```

---

## 5.8 修改 `_tts_segments_to_srt()`

把原来的 `_tts_segments_to_srt()` 替换成：

```python
    def _tts_segments_to_srt(self, segments: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        index = 1
        for segment in segments:
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = float(segment.get("target_start_seconds") or 0)
            end = float(segment.get("target_end_seconds") or 0)
            if end <= start:
                actual = float(segment.get("actual_duration_seconds") or 0)
                end = start + actual
            if end <= start:
                continue

            # 兼容旧模式
            if self._subtitle_mode() == "segment":
                blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
                index += 1
                continue

            chunks = self._split_subtitle_text(text)
            new_blocks, index = self._subtitle_chunks_to_srt_blocks(
                chunks=chunks,
                start=start,
                end=end,
                index_start=index,
            )
            blocks.extend(new_blocks)

        return "\n".join(blocks)
```

这是最关键的改动，因为当前正常流程优先走 `tts_segments`。

---

## 5.9 修改 burn-in 字幕样式

把 `pipeline.py` 第 2509 行附近的：

```python
style = "Fontsize=22,PrimaryColour=&H00000000,OutlineColour=&H00FFFFFF,BorderStyle=1,Outline=3,Shadow=0,Blur=1"
run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(current), "-vf", f"subtitles='{sub}':force_style='{style}'", "-c:a", "copy", str(output_path)])
```

改成：

```python
style = self._subtitle_force_style()
run_cmd([
    "ffmpeg",
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    str(current),
    "-vf",
    f"subtitles='{sub}':force_style='{style}'",
    "-c:a",
    "copy",
    str(output_path),
])
```

然后在 `PipelineRunner` 类里新增：

```python
    def _subtitle_force_style(self) -> str:
        cfg = self._subtitle_config()
        if self.options.aspect_ratio == "9:16":
            font_size = int(cfg.get("font_size_9_16", 20))
            margin_v = int(cfg.get("margin_v_9_16", 120))
        else:
            font_size = int(cfg.get("font_size_16_9", 24))
            margin_v = int(cfg.get("margin_v_16_9", 70))

        primary = str(cfg.get("primary_colour", "&H00000000"))
        outline_colour = str(cfg.get("outline_colour", "&H00FFFFFF"))
        outline = int(cfg.get("outline", 3))
        shadow = int(cfg.get("shadow", 0))

        return ",".join([
            f"Fontsize={font_size}",
            f"PrimaryColour={primary}",
            f"OutlineColour={outline_colour}",
            "BorderStyle=1",
            f"Outline={outline}",
            f"Shadow={shadow}",
            "Blur=1",
            "Alignment=2",
            f"MarginV={margin_v}",
        ])
```

说明：

- `Alignment=2`：底部居中。
- `MarginV=70`：字幕距离底部 70 像素，避免太贴边。
- `Fontsize` 变成配置项，后续可以按横屏 / 竖屏单独调。
- 由于我们已经在 SRT 生成阶段控制每条字幕长度，样式层不再承担“自动换行”的责任。

---

## 5.10 修改 `step_subtitles()` 的缓存 hash

当前 `input_hash` 只包含 voiceover 和 tts 版本：

```python
input_hash = stable_hash({"voiceover": self._step_version("voiceover_script"), "tts": self._step_version("tts")})
```

问题：你修改 `[subtitle]` 配置后，旧字幕缓存可能会被复用，导致看不到新效果。

建议改成：

```python
input_hash = stable_hash({
    "voiceover": self._step_version("voiceover_script"),
    "tts": self._step_version("tts"),
    "subtitle_config": self._subtitle_config(),
    "aspect_ratio": self.options.aspect_ratio,
})
```

这样只要字幕配置或画面比例变化，就会重新生成字幕。

---

## 6. 提示词优化方案

代码层修复后，字幕已经能稳定按句子显示。但为了让 TTS segment 本身更干净，建议同步优化提示词。

---

## 6.1 修改 `VOICEOVER_PROMPT`

在 `prompts.py` 的 `VOICEOVER_PROMPT` 中，找到：

```text
- 最后把所有 narration_segments 串联为 narration_text，TTS 和字幕都将使用 narration_text。
```

在这句后面追加：

```text
字幕友好规则：
- narration_segments[].text 不要写成大段长句，应尽量由 1-2 个完整短句组成。
- 每个短句建议 14-22 个汉字，最长不要超过 26 个汉字。
- 如果一个信息必须写得较长，请优先用逗号、分号拆成自然短从句，不要输出 35 个字以上的超长复合句。
- 字幕会按句号、问号、感叹号、逗号、分号、顿号切分；请保证切分后的每个短句仍然能独立理解。
- 不要为了压缩字数省略关键主语，例如“伊朗”“美方”“协议”“停火”等关键信息要保留。
```

---

## 6.2 修改 `VOICEOVER_LIGHT_PROMPT`

在 `VOICEOVER_LIGHT_PROMPT` 中，找到：

```text
5. narration_text 必须等于 narration_segments 的 text 串联结果，TTS 和字幕会使用它。
```

在后面追加：

```text
6. 字幕友好规则：narration_segments[].text 应由短句组成，每个短句建议 14-22 个汉字，最长不超过 26 个汉字；不要把多个事实堆成一个 35 字以上的长句。
7. 如果一句话过长，优先拆成多个自然短句或逗号短从句，保证每个短句单独显示时也能读懂。
```

---

## 6.3 可选：在 JSON schema 中增加字幕预览字段

如果希望后续调试更方便，可以在 `narration_segments` 里增加一个可选字段：

```json
"subtitle_lines": ["", ""]
```

但第一版不建议依赖这个字段。原因是：

- 模型可能不稳定输出。
- TTS 和字幕还是应以 `text` 为唯一主链路。
- 字幕拆分属于工程规则，放代码里更可控。

推荐第一版只改提示词，不增加新字段。

---

## 7. 最终效果示例

### 7.1 优化前 SRT

```srt
1
00:00:00,000 --> 00:00:06,000
近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。
```

渲染效果容易变成：

```text
近期美伊双方处于边谈边打的特殊状态，
伊朗革命卫队公开表态，若最终达成的停
火协议遭到违反，伊朗将立刻做出对等回应。
```

### 7.2 优化后 SRT

```srt
1
00:00:00,000 --> 00:00:01,650
近期美伊双方处于边谈边打的特殊状态，

2
00:00:01,650 --> 00:00:03,350
伊朗革命卫队公开表态，

3
00:00:03,350 --> 00:00:06,000
若停火协议遭到违反，伊朗将做出对等回应。
```

渲染效果会变成：

```text
近期美伊双方处于边谈边打的特殊状态，
```

随后切换：

```text
伊朗革命卫队公开表态，
```

再切换：

```text
若停火协议遭到违反，伊朗将做出对等回应。
```

也就是用户想要的“一句话一行 / 一次一行”。

---

## 8. 测试方案

### 8.1 单元测试：新增 `tests/test_subtitle_sentence_layout.py`

新增测试文件：

```python
from newsclip_agent.pipeline import PipelineRunner, RunOptions


def make_runner():
    return PipelineRunner(RunOptions(config="config.toml", skip_render=True))


def test_split_subtitle_text_prefers_sentence_and_clause():
    runner = make_runner()
    text = "近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。"
    chunks = runner._split_subtitle_text(text, max_chars=22)
    assert len(chunks) >= 3
    assert all(len(x) <= 22 for x in chunks)


def test_tts_segments_to_srt_generates_multiple_single_line_cues():
    runner = make_runner()
    segments = [
        {
            "text": "近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。",
            "target_start_seconds": 0,
            "target_end_seconds": 6,
            "actual_duration_seconds": 6,
        }
    ]
    srt = runner._tts_segments_to_srt(segments)
    assert "00:00:00" in srt
    # 至少拆成 2 个以上字幕块
    assert "\n2\n" in srt
    # 每个字幕文本行不应特别长
    text_lines = [line for line in srt.splitlines() if line and "-->" not in line and not line.isdigit()]
    assert all(len(line) <= 22 for line in text_lines)
```

注意：这个测试会实例化 `PipelineRunner`，如果你的项目初始化会读真实路径或外部模型，可以把 `_split_subtitle_text` 抽到独立模块，例如 `newsclip_agent/subtitle_policy.py`，测试会更轻。

更推荐的长期结构是：

```text
newsclip_agent/subtitle_policy.py
  split_subtitle_text()
  subtitle_chunks_to_srt_blocks()
  build_srt_from_tts_segments()
```

这样字幕逻辑可以独立测试，不依赖 PipelineRunner。

---

## 9. 更推荐的长期重构：新增 `subtitle_policy.py`

如果你愿意稍微规范一点，建议不要继续把所有逻辑塞进 `pipeline.py`，而是新增：

```text
newsclip_agent/subtitle_policy.py
```

里面放：

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .utils import srt_time, timecode_to_seconds
from .duration_policy import clean_voiceover_text


@dataclass(frozen=True)
class SubtitleSettings:
    mode: str = "sentence"
    max_chars_per_line_16_9: int = 22
    max_chars_per_line_9_16: int = 14
    min_cue_seconds: float = 1.0
    max_cue_seconds: float = 4.0


def load_subtitle_settings(raw: dict[str, Any]) -> SubtitleSettings:
    cfg = raw.get("subtitle", {}) or {}
    return SubtitleSettings(
        mode=str(cfg.get("mode", "sentence")),
        max_chars_per_line_16_9=int(cfg.get("max_chars_per_line_16_9", 22)),
        max_chars_per_line_9_16=int(cfg.get("max_chars_per_line_9_16", 14)),
        min_cue_seconds=float(cfg.get("min_cue_seconds", 1.0)),
        max_cue_seconds=float(cfg.get("max_cue_seconds", 4.0)),
    )
```

然后 `pipeline.py` 只负责调用：

```python
from .subtitle_policy import load_subtitle_settings, tts_segments_to_srt, narration_segments_to_srt, script_to_srt
```

这种方式更干净，但改动范围比直接在 `pipeline.py` 里加 helper 稍大。第一版建议先在 `pipeline.py` 内实现，确认效果后再抽模块。

---

## 10. 开发执行顺序

建议按这个顺序做，风险最低：

1. 在 `config.toml` 增加 `[subtitle]` 配置。
2. 在 `pipeline.py` 增加 `_subtitle_config()`、`_subtitle_max_chars_per_line()`、`_subtitle_force_style()` 等 helper。
3. 增加 `_split_subtitle_text()` 和 `_subtitle_chunks_to_srt_blocks()`。
4. 替换 `_script_to_srt()`、`_segments_to_srt()`、`_tts_segments_to_srt()`。
5. 修改 `step_subtitles()` 的 `input_hash`，让字幕配置变化时重新生成字幕。
6. 修改 burn-in 的 `force_style`，统一底部居中、字号、边距。
7. 修改 `VOICEOVER_PROMPT` 和 `VOICEOVER_LIGHT_PROMPT` 的字幕友好规则。
8. 跑一次 `subtitles + render`，不用重新跑完整视觉分析。

如果你的流程支持 `--use-version` 或只跑部分步骤，建议只重跑：

```bash
python run_pipeline.py --step subtitles --use-version voiceover_script:<旧版本> --use-version tts:<旧版本>
python run_pipeline.py --step render --use-version subtitles:<新版本>
```

如果当前 CLI 还不支持单步这样跑，就用已有的“从此步后续”按钮，从 `subtitles` 之后开始重跑即可。

---

## 11. 验收标准

本次优化完成后，用截图里的这类 AI 配音新闻视频检查：

1. 字幕区域同一时间默认只显示一行。
2. 单行字幕不会被 FFmpeg 自动折成 2-3 行。
3. 一条字幕一般 1-4 秒，不闪、不长时间不动。
4. 字幕切换点和 AI 配音语义停顿基本一致。
5. 横屏字幕不挡人物嘴部，底部距离舒适。
6. 修改 `[subtitle]` 配置后，重新跑 subtitles 会生成新 SRT，不会复用旧缓存。
7. 如果设置 `mode = "segment"`，可以回退到旧逻辑，方便排查问题。

---

## 12. 一句话结论

当前字幕展示不好，核心原因是代码把“镜头级 TTS segment”直接当成“屏幕字幕 cue”，导致一条字幕过长，FFmpeg 自动硬折行。正确做法是在生成 SRT 前新增“字幕短句拆分层”：按句子和自然停顿拆成短 cue，再按 TTS 实际时间分配展示时间，同时把字幕样式配置化。提示词可以辅助模型输出短句，但最终稳定性必须靠代码层强制拆分。
