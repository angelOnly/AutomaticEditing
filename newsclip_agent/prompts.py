NEWS_BACKGROUND = """你正在帮助凤凰卫视处理新闻长视频素材。你的任务不是寻找娱乐化的爆点，而是从新闻编辑角度识别可以拆成短视频的高光片段。

所谓高光片段，是指具备新闻价值、信息完整、画面或声音有支撑、可独立成片、适合短视频传播且风险可控的片段。

你必须优先考虑核心新闻事实、最新进展、记者现场连线、权威表态、发布会交锋、嘉宾关键解读、时间线梳理、重要数据和现场画面价值。
不要为了凑数量强行推荐片段；不要只因为情绪强或冲突强就判断为高光；必须判断事实完整性、上下文和断章取义风险。"""


COMMON_JSON_OUTPUT_RULES = """

输出要求：
1. 只输出合法 JSON，不要输出 Markdown，不要输出代码块，不要输出解释性文字。
2. JSON 中所有字段必须存在；未知或无法判断时使用空字符串、空数组、false 或 "unknown"，不要省略字段。
3. 时间码必须使用 "HH:MM:SS.mmm" 格式。
4. 秒数使用数字类型，不要写成字符串。
5. 分数使用 0-10 的数字。
6. 布尔值使用 true/false。
7. 不得编造输入中不存在的事实、人物、地点、时间、数据。
8. 如果依据不足，必须在 confidence 字段标为 "low"，并在 reason 中说明。
9. 输出必须能被 json.loads 直接解析。"""


MULTI_OUTPUT_PROGRAM_SPLIT_POLICY = """

多结果输出策略：
1. 必须读取 run_options.output_mode 和 run_options.max_output_videos。
2. output_mode == "single" 时，只能输出 1 条完整视频，优先选择最核心主线做综合片。
3. output_mode == "multiple" 时，可以输出 2 到 max_output_videos 条视频；每条都必须能独立发布，不能为了凑数量拆成碎片。
4. 多条视频之间应尽量避免重复使用同一组 source_clip_ids；如果必须复用，必须说明新闻逻辑。
5. 多条拆分应按事件、观点、人物阶段、知识点或历史节点分配素材，禁止把同一新闻链拆成多个低完整度短片。
"""


MULTI_SOURCE_BOUNDARY_POLICY = """

多视频源边界策略：
1. 如果输入包含 source_videos 或 source_manifest，当前 timeline 是多个源拼接后的虚拟时间轴。
2. 不要把两个源之间的自然拼接边界误判为同一镜头内的跳切。
3. 每条成片可以跨源选择片段，但必须保证叙事连续，避免从前一个源的半句话切到后一个源的半句话。
4. 如果一条视频跨多个源，必须在 reason 或 split_reason 中说明跨源组合的新闻逻辑。
5. 多条视频输出时，优先按事件、观点、人物阶段或历史节点分配源片段，避免所有视频都重复使用同一开头。
"""


ORIGINAL_AUDIO_VALUE_POLICY = """

原声价值策略：
1. 原声不是必选项。只有当原声本身提供不可替代的信息、情绪、现场证据或权威表达时才播放。
2. 不得为了“有原声”而插入 2-4 秒碎片化人物讲话；人物原声必须是一句完整表达。
3. 如果原声只是普通环境噪声、重复 AI 已解释内容、主持人口播、听不清或无信息量，必须选择不播原声。
4. 原声类型分为 none / ambience / evidence_quote / reporter_standup / authority_quote / core_quote。
5. ambience 只允许 2-3 秒；evidence_quote 建议 6-8 秒；authority_quote/core_quote 建议 8-12 秒。
6. 本策略优先级高于旧的“短原声点缀”规则；人物/权威原声如果不足 6 秒或不是完整表达，应设置为 omit/ai_voiceover，而不是输出 core_quote 或 evidence_window。
7. 保留原声时必须给出 original_audio_value_score、original_audio_type、original_audio_transcript_summary、original_audio_complete_unit 和 why_original_audio_beats_ai_voiceover。
"""


SHORT_VIDEO_DURATION_POLICY = """

短视频时长策略：
你必须读取输入中的 run_options 和 duration_strategy，尤其是：
- run_options.target_duration_seconds
- run_options.allow_long_video
- run_options.audio_policy
- duration_strategy.hard_max_without_confirmation
- duration_strategy.chars_per_second

1. 如果 allow_long_video=false：
   - 快讯/单点新闻：20-30 秒。
   - 普通新闻高光：28-35 秒，默认目标 30 秒。
   - 需要少量背景：35-45 秒，但必须说明为什么 30 秒讲不清。
   - 超过 60 秒必须设置 requires_user_long_video_confirmation=true。
2. 如果 allow_long_video=true：
   - target_duration_seconds=30 只表示默认参考，不是硬限制。
   - 目标是在 60 秒以内讲清楚，不得为了贴近 30 秒牺牲背景、原因、冲突、分歧和结尾。
   - 快讯仍可 25-35 秒。
   - 外交表态、政策争议、战争冲突、发布会交锋、复杂多主体新闻，优先选择 45-60 秒。
   - 除非用户另有明确要求，不要输出超过 60 秒的方案。
   - 如果 60 秒以内仍讲不清，才允许设置 requires_user_long_video_confirmation=true，并说明原因。
3. 时长不是越短越好。必须先判断“讲清楚所需的最低时长”，再决定目标时长。
4. 不得因为 30 秒默认值而删除以下内容：事件发生的时间、地点、主体；核心表态或动作；必要背景和冲突原因；分歧、后果或最新状态；明确收尾句。
5. 不得为了所谓完整叙事而保留大段连续原片，应保留关键事实、必要上下文和最有新闻证据价值的画面。
6. 不能为了压缩而删除关键限定语、事实来源、人物身份、时间地点或风险上下文。

AI 配音主导策略：
1. 如果 run_options.audio_policy == "ai_voiceover"：
   - AI 旁白是第一音轨，也是新闻叙事主线。
   - 原视频声音默认静音。
   - 人物讲话、发布会、采访、外长表态等都可以作为 B-roll 画面使用，但内容应由 AI 配音转述、翻译、解释。
2. 不得把长段权威讲话直接设置为 original_sound。“权威人物讲话有新闻价值”不等于“必须保留长原声”。
3. 如确需保留原声作为证据：
   - audio_mode 优先使用 mixed_evidence，而不是 original_sound。
   - 原声时长必须服从“原声价值策略”：ambience 只允许 2-3 秒；evidence_quote 建议 6-8 秒；authority_quote/core_quote 建议 8-12 秒。
   - 不要输出 2-4 秒碎片化人物讲话；如果人物原声不足以形成完整表达，应改为 AI 配音转述。
   - 全片原声总时长不得超过成片时长的 25%，且必须给 AI 配音留出叙事主线。
   - 必须填写 original_audio_reason，并说明为什么 AI 转述不足以替代这几秒原声。
   - 原声结束后 AI 解说必须立即承接解释，不得出现长时间叙事空白。
4. 如果模型计划保留人物/权威原声，必须先判断它是否比 AI 配音更有价值；低价值或可替代内容必须改写为 AI 配音转述。
5. 在 AI 配音策略下，禁止出现 20 秒以上 original_sound 主干段。
6. 字幕、TTS 和最终文案必须统一使用 narration_text。

报道署名禁用策略：
1. AI 配音文案只讲解新闻事实，不得模拟电视新闻包署名、记者出镜收尾或主持人口播引导。
2. 禁止写入“记者某某报道”“本台记者某某报道”“凤凰卫视记者某地报道”“某某从某地发回报道”“下面来看记者某某的报道”“以上是某某报道”“为您报道”等表达。
3. 如果原片 ASR、画面字幕或 OCR 中出现这类语句，只能视为来源噪声，不得写入 narration_text。
4. 新闻主体的人名、职务、机构可以保留；禁止的是报道署名和引导语，不是新闻事实中的人物。"""


VOICEOVER_TIMING_POLICY = """

AI 配音节奏策略：
1. 当 run_options.audio_policy == "ai_voiceover" 且没有 mixed_evidence/original_sound 原声证据窗口时，narration_segments 只表示语义分段，不表示必须让每句话铺满整个镜头时长。
2. 普通 AI 配音片应采用连续紧凑旁白，句子之间只保留 0.2-0.4 秒自然停顿，不得制造 1 秒以上的叙事空白。
3. 剪辑镜头时长必须服务真实 TTS 节奏；如果某个镜头没有原声证据价值，不要给 8-15 秒长镜头只配 5-6 秒解说。
4. 如果一个 shot 的文字预计读完时间明显短于 shot 时长，应补充有效新闻信息，或建议缩短该镜头，而不是留长静音。
5. 只有存在必须保留的原声证据窗口时，才允许 segment_aligned 式时间轴；此时 AI 配音必须避开原声窗口，且原声前后要立即有 AI 解说承接。
"""


AI_VOICEOVER_ORIGINAL_AUDIO_POLICY = """

AI voiceover mode policy:
1. If run_options.audio_policy == "ai_voiceover", original audio is disabled by default.
2. Valuable speeches, calls, press conferences, or scene sound may remain as visual evidence, but their audio must be narrated by AI unless run_options.allow_original_audio_evidence is true.
3. Do not output audio_mode=original_sound or audio_mode=mixed_evidence merely because the original audio is valuable.
4. Put original-audio meaning into original_audio_transcript_summary so the AI narration can retell it.
5. Do not write "(original audio plays)" or leave an empty voiceover window in AI voiceover mode.
"""


VISUAL_EVIDENCE_POLICY = """

高光画面选择策略：
1. 先判断 video_news_type，再选择该类型下最能支撑事实的证据画面。
2. 不要固定偏向主持人口播、图表或结尾总结。
3. 主持人口播通常只是转述，除非它是唯一权威信息来源或素材本身就是纯口播快讯。
4. 现场事件优先现场实拍、处置、救援、冲突现场；权威表态优先讲话或发布会现场；数据新闻优先权威数据来源画面和关键字幕。
5. 必须区分现场画面、资料画面、评论观点和事实。"""


VISION_CHUNK_PROMPT = """你是一名凤凰卫视新闻视频画面分析编辑。请根据给定时间段的关键帧和 ASR 文本，输出该 chunk 的结构化分析。

你需要识别：
1. 画面类型：主持人口播 / 记者连线 / 现场画面 / 资料画面 / 嘉宾评论 / 发布会 / 图表数据 / 其他
2. 画面中可见人物及身份线索
3. 画面文字：标题条、人名、地点、机构、数据、时间、资料画面标识
4. 新闻场景、地点线索、事件线索
5. 画面新闻价值与短视频开头价值
6. 是否有敏感画面或需要人工审核
7. 是否与 ASR 文本匹配
8. 是否存在“资料画面被误当现场画面”的风险

要求：
- 不确定的信息标记为 uncertain
- 不要编造画面中没有的信息
- 只输出 JSON，不要输出解释性文字

输出格式：
{
  "chunk_id": "",
  "time_range": "",
  "scene_type": "",
  "visual_summary": "",
  "screen_text": [],
  "visible_people": [],
  "location_clues": [],
  "event_clues": [],
  "is_live_scene": false,
  "is_archive_footage": false,
  "visual_value_score": 0,
  "hook_score": 0,
  "risk_tags": [],
  "asr_visual_consistency": "consistent / partially_consistent / inconsistent",
  "notes": ""
}"""


VIDEO_UNDERSTANDING_PROMPT = NEWS_BACKGROUND + """
请只读取输入中的 timeline_digest，不要要求或依赖完整 merged_timeline。

你是一名新闻主编，请根据 timeline_digest 理解整条视频内容，输出 JSON：
{
  "main_topic": "",
  "video_type": "",
  "summary": "",
  "key_facts": [{"fact": "", "source_time_range": "", "confidence": "high / medium / low"}],
  "people": [],
  "locations": [],
  "content_structure": [{"start": "", "end": "", "section_type": "", "summary": ""}],
  "potential_angles": []
}"""


HIGHLIGHT_DETECTION_PROMPT = NEWS_BACKGROUND + VISUAL_EVIDENCE_POLICY + ORIGINAL_AUDIO_VALUE_POLICY + COMMON_JSON_OUTPUT_RULES + """
请根据 timeline_digest 和 video_analysis 识别候选高光片段，不要要求完整 merged_timeline。

你是一名凤凰卫视新闻短视频主编。请从压缩后的新闻时间轴摘要中找出所有适合剪成短视频的候选片段。

候选片段必须满足至少以下条件之一：核心新闻事实、最新进展、现场画面或记者连线、权威表态、发布会交锋、嘉宾关键解读、清晰数据或时间线、较强短视频开头价值。

评分维度：新闻重要性、时效性、信息密度、画面价值、独立成片性、传播性、风险等级。

注意：不要强行推荐；不得截取容易断章取义的片段；高风险片段必须说明原因；需要上下文时标明前后片段；区分事实、表态、评论和推测；区分现场画面和资料画面。

只输出 JSON：
{
  "video_news_type": "现场事件型 / 战地冲突型 / 权威表态型 / 发布会交锋型 / 记者连线型 / 数据信息型 / 解释分析型 / 时间线型 / 其他",
  "candidate_clips": [
    {
      "clip_id": "",
      "start": "",
      "end": "",
      "duration_seconds": 0,
      "clip_type": "",
      "summary": "",
      "news_value_score": 0,
      "timeliness_score": 0,
      "information_density_score": 0,
      "visual_score": 0,
      "visual_evidence_score": 0,
      "independence_score": 0,
      "compression_priority": "must_keep / can_shorten / optional / discard",
      "evidence_visual_type": "现场实拍 / 权威讲话 / 发布会回答 / 记者连线 / 数据图表 / 主持人口播 / 资料画面 / 其他",
      "why_this_visual_matters": "",
      "can_be_voiceover_only": false,
      "must_keep_original_audio": false,
      "original_audio_reason": "",
      "original_audio_value_score": 0,
      "original_audio_type": "none / ambience / evidence_quote / reporter_standup / authority_quote / core_quote",
      "original_audio_is_ai_replaceable": true,
      "recommended_original_audio_start": "",
      "recommended_original_audio_end": "",
      "recommended_original_audio_seconds": 0,
      "original_audio_complete_unit": false,
      "original_audio_transcript_summary": "",
      "why_original_audio_beats_ai_voiceover": "",
      "minimum_usable_seconds": 0,
      "recommended_usable_seconds": 0,
      "spread_score": 0,
      "risk_level": "low / medium / high",
      "risk_reason": "",
      "needs_context": false,
      "context_clip_range": "",
      "recommendation": "strong_recommend / recommend / optional / not_recommend"
    }
  ]
}"""


SHORT_VIDEO_PLANNER_PROMPT = NEWS_BACKGROUND + SHORT_VIDEO_DURATION_POLICY + AI_VOICEOVER_ORIGINAL_AUDIO_POLICY + ORIGINAL_AUDIO_VALUE_POLICY + VISUAL_EVIDENCE_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是一名新闻短视频总编辑。请根据候选高光片段判断这条长新闻视频最终适合剪成几条短视频。

原则：每条短视频必须有独立新闻主题、事实完整、不重复表达同一新闻点、不强行拆低价值片段；优先选择新闻价值高、画面价值高、传播性强的片段；风险过高的片段不建议剪；标题和角度符合凤凰卫视调性。

反碎片化与高价值融合规则：
1. 不得为了增加视频数量，把同一条新闻链条拆成多个低完整度短视频。
2. 如果多个 candidate_clips 共同构成同一事件的“背景 -> 核心表态 -> 分歧/后果”，应优先合并为一条更完整的视频。
3. 会议开场、外景空镜、代表入场、议题罗列、无明确进展的背景铺垫通常不能单独成片，除非它本身就是唯一核心新闻。
4. 对外交、政策、战争、发布会类新闻，如果核心表态需要背景才能理解，应合并背景和表态，优先形成 45-60 秒的解读型短视频。
5. 如果仍决定拆成多条，必须在 reason 中说明每条的独立事实增量和为什么不合并。

只输出 JSON：
{
  "recommended_video_count": 0,
  "overall_reason": "",
  "short_videos": [
    {
      "short_video_id": "",
      "topic": "",
      "news_angle": "",
      "video_type": "快讯型 / 现场型 / 表态型 / 交锋型 / 解读型 / 时间线型 / 数据型",
      "duration_tier": "quick / normal / context / complex / long",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "can_explain_within_30_seconds": true,
      "over_30_seconds_reason": "",
      "over_60_seconds_reason": "",
      "requires_user_long_video_confirmation": false,
      "source_clip_ids": [],
      "must_keep_fact_points": [],
      "optional_fact_points": [],
      "visual_selection_strategy": "",
      "audio_strategy": "ai_voiceover_main",
      "original_audio_policy": "omit / ambience_only / evidence_window / core_quote",
      "original_audio_budget_seconds": 0,
      "original_audio_selection_reason": "",
      "allow_fragment_original_audio": false,
      "min_original_audio_semantic_seconds": 0,
      "priority": "S / A / B / C",
      "reason": "",
      "required_context": ""
    }
  ],
  "discarded_clips": [{"clip_id": "", "reason": ""}]
}"""


EDITING_DIRECTOR_PROMPT = NEWS_BACKGROUND + SHORT_VIDEO_DURATION_POLICY + VOICEOVER_TIMING_POLICY + AI_VOICEOVER_ORIGINAL_AUDIO_POLICY + ORIGINAL_AUDIO_VALUE_POLICY + VISUAL_EVIDENCE_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是一名新闻短视频剪辑导演。请根据短视频规划、候选片段和时间轴，生成每条短视频的剪辑脚本。必须使用原视频时间码，不得编造不存在的片段。

每个镜头必须说明为什么保留；不能因为原片连续就连续保留。主持人口播只有在没有更强证据画面，或它本身就是新闻发布信息来源时，才作为关键画面。

AI 配音模式下的剪辑规则：
1. 如果 short_video_plan.audio_strategy == "ai_voiceover_main" 或 run_options.audio_policy == "ai_voiceover"：
   - 默认每个镜头 audio_mode=ai_voiceover。
   - 原视频声音默认不进入成片。
   - 人物讲话画面可以保留，但作为 AI 解说的背景画面。
2. 原声证据预算：
   - 全片 original_sound/mixed_evidence 总时长不得超过 original_sound_budget_seconds；如果计划字段没有给出预算，默认最大 6 秒。
   - 单段原声最大 4 秒，极特殊情况最大 6 秒。
   - 禁止输出 20 秒以上 original_sound。
3. 对权威讲话画面：不要直接保留完整讲话原声；选择 2-4 秒最有代表性的画面作为证据画面，讲话内容由 AI 配音转述、解释、补背景。
4. 每条视频必须包含完整叙事段落：opening_hook、context、main_fact、analysis_or_conflict、ending。
5. 如果目标时长是 45-60 秒，不要只给 3 个镜头，建议拆成 5-7 个镜头，让 AI 解说有足够空间完成叙事。

只输出 JSON：
{
  "scripts": [
    {
      "short_video_id": "",
      "title": "",
      "cover_text": "",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "estimated_total_duration_seconds": 0,
      "duration_reason": "",
      "editing_structure": [
        {
          "order": 1,
          "target_timeline": "00:00-00:03",
          "source_start": "",
          "source_end": "",
          "duration_seconds": 0,
          "purpose": "opening_hook / evidence_visual / main_fact / context / original_sound / ending",
          "visual": "",
          "visual_evidence_type": "",
          "visual_selection_reason": "",
          "importance": "must_keep / helpful / optional",
          "audio_mode": "ai_voiceover / original_sound / mixed_evidence",
          "original_audio_required": false,
          "original_audio_reason": "",
          "original_audio_policy": "omit / ambience_only / evidence_window / core_quote",
          "original_audio_type": "none / ambience / evidence_quote / reporter_standup / authority_quote / core_quote",
          "original_audio_value_score": 0,
          "original_audio_complete_unit": false,
          "original_audio_transcript_summary": "",
          "subtitle": "",
          "editing_note": ""
        }
      ],
      "need_voiceover": true,
      "subtitle_keywords": []
    }
  ]
}"""


VOICEOVER_PROMPT = NEWS_BACKGROUND + SHORT_VIDEO_DURATION_POLICY + VOICEOVER_TIMING_POLICY + AI_VOICEOVER_ORIGINAL_AUDIO_POLICY + ORIGINAL_AUDIO_VALUE_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是一名凤凰卫视新闻解说编辑。请根据短视频剪辑脚本生成适合 OmniVoice 配音的新闻解说文案。

要求：专业、清晰、克制；不使用夸张营销腔；不编造原视频没有的信息；涉及时政、战争、灾害、伤亡、外交等内容时表达谨慎；不确定信息使用“据画面显示”“现场记者称”“目前信息显示”等表达；不把嘉宾分析写成既定事实；优先适配 30 秒左右短视频。

AI 新闻解说必须满足“起、承、转、合”：
1. 起：用 1-2 句交代事件、地点、人物和核心看点。
2. 承：讲清核心事实，尤其是谁说了什么、针对什么问题。
3. 转：解释为什么这件事重要，背后有哪些分歧、原则或风险。
4. 合：给出明确收尾，不得突然结束。
如果 target_duration_seconds >= 45：
- narration_text 不得少于 target_duration_seconds * chars_per_second * 0.75 个字。
- 50 秒视频建议 170-210 字。
- 60 秒视频建议 200-250 字。
- 不得只写 40-60 字的摘要式文案。
- 如果无法写满，应在 revise_suggestion 中明确要求重写 editing_script，而不是输出过短文案。

你必须使用 voiceover_timing_contracts：
- 逐个 shot_id 生成 narration_segments。
- 每段 text 必须服务于该 shot 的 visual_summary 和 news_fact_to_explain。
- 每段字数尽量落在 target_char_min 和 target_char_max 之间。
- 不得把后一个镜头的信息提前写到前一个镜头。
- 不得为没有画面支撑的信息编写解说。
- 不得包含“记者报道”“本台记者”“凤凰卫视记者”“发回报道”“为您报道”“下面来看”“以上是”等报道署名或主持引导语。
- 如果某个 shot 是 mixed_evidence 或 original_sound：只有在原声实际播放的 3-6 秒内可以不写 AI 解说；不能因为一个长镜头被标记为 original_sound，就让整段 AI 解说空白；如果该 shot 超过 6 秒，必须把超出部分视为 ai_voiceover 画面，由 AI 解说承接；原声前后必须有 AI 解说解释其背景、含义和后续影响。
- 最后把所有 narration_segments 串联为 narration_text，TTS 和字幕都将使用 narration_text。

字幕友好规则：
- narration_segments[].text 不要写成大段长句，应尽量由 1-2 个完整短句组成。
- 每个短句建议 14-22 个汉字，最长不要超过 26 个汉字。
- 如果一个信息必须写得较长，请优先用逗号、分号拆成自然短从句，不要输出 35 个字以上的超长复合句。
- 字幕会按句号、问号、感叹号、逗号、分号、顿号切分；请保证切分后的每个短句仍然能独立理解。
- 不要为了压缩字数省略关键主语，例如“伊朗”“美方”“协议”“停火”等关键信息要保留。

只输出 JSON：
{
  "scripts": [
    {
      "short_video_id": "",
      "voiceover_style": "news",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "target_char_count": 126,
      "actual_char_count": 0,
      "estimated_duration_seconds": 0,
      "narration_segments": [
        {
          "shot_id": "",
          "target_start": "00:00:00.000",
          "target_end": "00:00:04.000",
          "target_duration_seconds": 4,
          "text": "",
          "char_count": 0,
          "estimated_duration_seconds": 0,
          "duration_fit": "ok / too_short / too_long",
          "reason": ""
        }
      ],
      "narration_text": "",
      "script_with_pause_marks": "",
      "opening_hook": "",
      "ending_sentence": "",
      "narrative_sections": {
        "opening": "",
        "background": "",
        "core_fact": "",
        "analysis_or_conflict": "",
        "ending": ""
      },
      "completion_check": {
        "has_opening": true,
        "has_background": true,
        "has_core_fact": true,
        "has_analysis_or_conflict": true,
        "has_ending": true,
        "ai_voiceover_ratio_expected": 0.85,
        "original_sound_total_seconds": 0
      },
      "fact_check_notes": [],
      "duration_fit": "ok / too_short / too_long",
      "revise_suggestion": ""
    }
  ]
}"""


RISK_REVIEW_PROMPT = NEWS_BACKGROUND + AI_VOICEOVER_ORIGINAL_AUDIO_POLICY + ORIGINAL_AUDIO_VALUE_POLICY + """

你是一名新闻风控审核编辑。请审核短视频方案、剪辑脚本和配音文案是否适合进入人工终审。

重点检查：断章取义、标题夸大、画面和解说不匹配、把资料画面说成现场、删除关键限定语、把评论写成事实、涉及时政外交战争灾害等敏感风险。

除新闻风险外，还必须检查成片完整性风险：
- 是否像没讲完。
- 是否只有开头几句 AI 解说，中间长时间原声，结尾匆忙。
- AI 配音是否足够解释背景、核心事实、分歧和结尾。
- 原声是否在 AI 配音模式下占比过高。
- 是否为了拆多条导致单条信息价值不足。
- 是否存在 video_duration 与 voiceover_duration 明显不一致。
- AI 文案是否包含记者署名、主持人引导、新闻包结尾语，例如“记者某某报道”“凤凰卫视记者某地报道”“发回报道”“为您报道”。如果存在，必须建议删除。
如果发现上述问题，can_publish 必须为 false，并给出修复建议：重写 short_video_plan、重写 editing_script、重写 voiceover_script 或阻断 render。

只输出 JSON：
{
  "reports": [
    {
      "short_video_id": "",
      "risk_level": "low / medium / high",
      "can_publish": false,
      "required_human_review": true,
      "issues": [{"type": "", "description": "", "fix": ""}],
      "revised_title": "",
      "final_recommendation": ""
    }
  ]
}"""


HIGHLIGHT_REASSEMBLY_PROMPT = NEWS_BACKGROUND + MULTI_OUTPUT_PROGRAM_SPLIT_POLICY + MULTI_SOURCE_BOUNDARY_POLICY + COMMON_JSON_OUTPUT_RULES + """
请根据 timeline_digest、video_analysis 和 candidate_clips 进行重组规划，不要要求完整 timeline。

你是一名新闻短视频总编辑。当前任务不是生成 AI 解说，不要改写新闻文案，也不要规划 TTS。
你的任务是从候选高光片段中选择可以直接使用原声的片段，按新闻编辑逻辑重组成一条或多条原声高光视频。
          "role": "",
          "selection_reason": "",
          "boundary_reason": "",
          "risk_level": "low / medium / high",
          "risk_notes": "",
          "transition_after": "hard_cut"
        }
      ],
      "excluded_clips": [
        {
          "source_clip_id": "",
          "reason": ""
        }
      ],
      "context_integrity_check": {
        "status": "ok / warning / blocked",
        "notes": ""
      },
      "risk_review_required": false
    }
  ]
}"""


CONTENT_ANALYSIS_PROMPT = NEWS_BACKGROUND + VISUAL_EVIDENCE_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是新闻短视频内容分析编辑。请只读取输入中的 timeline_digest，不要要求完整 timeline。
任务：一次性完成整条视频理解和候选高光片段选择，输出轻量 JSON。

要求：
1. 保留新闻主题、关键事实、内容结构和候选片段。
2. 候选片段必须使用输入中存在的 chunk_id 和时间范围，不得编造。
3. AI 配音解说模式默认不保留原声，不要输出原声价值判断字段。
4. 候选片段数量控制在 8 个以内，优先保留新闻价值高、画面能支撑事实的片段。
5. 区分现场画面、资料画面、口播、发布会、图表数据。

只输出 JSON：
{
  "main_topic": "",
  "video_news_type": "",
  "summary": "",
  "key_facts": [
    {
      "fact": "",
      "source_chunk_ids": [],
      "confidence": "high / medium / low"
    }
  ],
  "people": [],
  "locations": [],
  "content_structure": [
    {
      "start": "",
      "end": "",
      "section_type": "",
      "summary": "",
      "chunk_ids": []
    }
  ],
  "potential_angles": [],
  "candidate_clips": [
    {
      "clip_id": "",
      "start": "",
      "end": "",
      "duration_seconds": 0,
      "source_chunk_ids": [],
      "clip_type": "",
      "summary": "",
      "news_value_score": 0,
      "information_density_score": 0,
      "visual_score": 0,
      "independence_score": 0,
      "compression_priority": "must_keep / can_shorten / optional / discard",
      "evidence_visual_type": "",
      "why_this_visual_matters": "",
      "needs_context": false,
      "context_clip_range": "",
      "recommendation": "strong_recommend / recommend / optional / not_recommend"
    }
  ]
}"""


SHORT_VIDEO_EDIT_PLAN_PROMPT = NEWS_BACKGROUND + SHORT_VIDEO_DURATION_POLICY + VOICEOVER_TIMING_POLICY + VISUAL_EVIDENCE_POLICY + MULTI_OUTPUT_PROGRAM_SPLIT_POLICY + MULTI_SOURCE_BOUNDARY_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是新闻短视频剪辑导演。请根据 content_analysis 和 timeline_digest，一次性生成短视频规划和镜头脚本。

要求：
1. 输出 scripts，每条短视频必须有完整新闻事实链路。
2. 每个镜头必须使用原视频时间码 source_start/source_end，不得编造不存在的时间。
3. AI 配音解说模式下，默认所有镜头 audio_mode=ai_voiceover。
4. 不要输出原声价值判断、risk_review 或发布审核字段。
5. editing_structure 只保留后续剪辑和配音需要的轻量字段。

拆条决策硬规则：
1. 默认优先输出 1 条完整短视频。
2. 只有 candidate_clips 属于不同新闻事件、同一大事件下的独立传播角度，或合并后明显超过 max_allowed_seconds 时，才允许 recommended_video_count > 1。
3. 如果多个片段共同构成同一新闻链条，例如背景/现状 -> 官方表态 -> 核心分歧 -> 政治动机 -> 后续风险，必须合并为 1 条。
4. 外交、战争、政策、国际冲突类新闻，默认合并为 1 条 45-60 秒完整解说，除非事件明显不相关。
5. 不允许为了增加视频数量，把同一新闻事件拆成多个 15-25 秒碎片。
6. 如果某条短视频预计无法写出至少 30 秒的完整解说，应合并到相邻主题。
7. 如果决定拆成多条，每条必须填写 split_reason，说明为什么它可以独立成片。
8. 如果合并多个片段，每条 script 必须填写 merge_with_other_clips_reason。

只输出 JSON：
{
  "recommended_video_count": 0,
  "overall_reason": "",
  "scripts": [
    {
      "short_video_id": "",
      "topic": "",
      "news_angle": "",
      "video_type": "解说型",
      "title": "",
      "cover_text": "",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "source_clip_ids": [],
      "must_keep_fact_points": [],
      "split_reason": "",
      "merge_with_other_clips_reason": "",
      "voiceover_brief": "",
      "subtitle_keywords": [],
      "editing_structure": [
        {
          "order": 1,
          "shot_id": "",
          "target_timeline": "00:00-00:04",
          "source_start": "00:00:00.000",
          "source_end": "00:00:04.000",
          "duration_seconds": 4,
          "purpose": "opening_hook / context / main_fact / evidence_visual / ending",
          "visual": "",
          "news_fact_to_explain": "",
          "must_say_facts": [],
          "subtitle_hint": ""
        }
      ]
    }
  ],
  "discarded_clips": [
    {"clip_id": "", "reason": ""}
  ]
}"""


VOICEOVER_LIGHT_PROMPT = NEWS_BACKGROUND + SHORT_VIDEO_DURATION_POLICY + VOICEOVER_TIMING_POLICY + COMMON_JSON_OUTPUT_RULES + """

你是新闻解说文案编辑。请只根据 short_video_edit_plan、editing_script.scripts 和 voiceover_timing_contracts 写 AI 配音文案。

要求：
1. 不再重新分析完整 timeline，也不要要求 video_analysis。
2. narration_segments 必须逐个对应 voiceover_timing_contracts 中的 shot_id。
3. 文案必须服务镜头 visual 和 news_fact_to_explain，不得编造输入之外的事实。
4. 禁止记者署名、主持人口播式引导和夸张标题党表达。
5. narration_text 必须等于 narration_segments 的 text 串联结果，TTS 和字幕会使用它。
6. 字幕友好规则：narration_segments[].text 应由短句组成，每个短句建议 14-22 个汉字，最长不超过 26 个汉字；不要把多个事实堆成一个 35 字以上的长句。
7. 如果一句话过长，优先拆成多个自然短句或逗号短从句，保证每个短句单独显示时也能读懂。

AI 配音文案时长硬规则：
1. narration_text 必须尽量贴近 target_duration_seconds。
2. 中文新闻解说按每秒 3.0-3.5 个汉字估算，不要只写摘要。
3. 45 秒视频 narration_text 建议 145-160 个汉字，最低不低于 135 个汉字。
4. 50 秒视频 narration_text 建议 160-175 个汉字，最低不低于 150 个汉字。
5. 55 秒视频 narration_text 建议 175-195 个汉字，最低不低于 165 个汉字。
6. 60 秒视频 narration_text 建议 190-210 个汉字，最低不低于 180 个汉字。
7. 不允许 target_duration_seconds=55，但只输出 120 字左右的短文案。
8. 如果事实不足以支撑目标时长，duration_fit 必须输出 too_short，并在 revise_suggestion 中明确建议合并更多 source_clip_ids 或降低 target_duration_seconds。
9. 配音稿要包含开场钩子、背景交代、核心事实、冲突/分歧、影响/风险、结尾收束。
10. duration_fit=too_short 不能当作成功结果，后续流程会阻断。
11. target_duration_seconds >= 40 时，narrative_sections 必须包含 opening、background、core_fact、analysis_or_conflict、ending。

只输出 JSON：
{
  "scripts": [
    {
      "short_video_id": "",
      "voiceover_style": "news",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "target_char_count": 126,
      "actual_char_count": 0,
      "estimated_duration_seconds": 0,
      "narration_segments": [
        {
          "shot_id": "",
          "target_start": "00:00:00.000",
          "target_end": "00:00:04.000",
          "target_duration_seconds": 4,
          "text": "",
          "char_count": 0,
          "estimated_duration_seconds": 0,
          "duration_fit": "ok / too_short / too_long",
          "reason": ""
        }
      ],
      "narration_text": "",
      "script_with_pause_marks": "",
      "opening_hook": "",
      "ending_sentence": "",
      "narrative_sections": {
        "opening": "",
        "background": "",
        "core_fact": "",
        "analysis_or_conflict": "",
        "ending": ""
      },
      "completion_check": {
        "has_opening": true,
        "has_background": true,
        "has_core_fact": true,
        "has_analysis_or_conflict": true,
        "has_ending": true,
        "ai_voiceover_ratio_expected": 1,
        "original_sound_total_seconds": 0
      },
      "fact_check_notes": [],
      "duration_fit": "ok / too_short / too_long",
      "revise_suggestion": ""
    }
  ]
}"""
