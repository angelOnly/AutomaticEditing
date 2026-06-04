请读取这个 GitHub 工程代码，不要只用代码搜索，优先按路径读取文件，因为 GitHub 搜索索引可能不稳定。

仓库地址：
https://github.com/angelOnly/AutomaticEditing

仓库全名：
angelOnly/AutomaticEditing

分支：
clean-highlight-reassembly

项目入口：
run_web.py

请先按路径读取这些文件：
1. README.md  代码改动太大，不要读这个，直接读代码
2. run_web.py
3. web_app.py
4. config.toml
5. run_pipeline.py
6. newsclip_agent/pipeline.py

然后根据 run_web.py、web_app.py、newsclip_agent/pipeline.py 里的 import 和调用关系，继续追踪相关模块。

这个项目是“凤凰新闻视频智能拆条系统”，主要包含：
- Web 工作台
- 视频预处理
- FunASR 转写
- 视觉 chunk 分析
- ASR 与画面理解融合时间轴
- 高光识别
- 短视频规划
- AI 解说文案
- OmniVoice 配音
- 字幕生成
- FFmpeg 粗剪
- 高光重组模式

请基于实际代码分析，不要只给泛泛建议。需要指出：
1. 当前代码现状
2. 问题原因
3. 需要修改哪些文件
4. 每个文件具体怎么改
5. 给出关键伪代码或可直接替换的代码片段
6. 注意兼容现有 Web 启动方式：python run_web.py
7. 给出可执行的，详细具体的技术实现方案