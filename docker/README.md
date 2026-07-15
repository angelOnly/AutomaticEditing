# Docker 打包说明

这个目录用于把运行环境、模型和参考音频打进 Docker 镜像。代码、视频素材和输出目录在运行时从宿主机挂载，方便更新代码和替换素材。

封面生成使用的 Seedream Skill 是随代码工程提交的完整技能包：
`newsclip_agent/seedream-skills/SKILL.md`（包含 `SKILL.md`、README 和 `scripts/`）。
豆包 2.1 会在请求的 `system` 消息中注入工程内这份 `SKILL.md`，再生成 Seedream 提示词；容器不会读取宿主机用户目录下的 Codex/Agent skills。入口脚本会在启动时检查该技能包，缺失会直接报错。
Skill 路径、版本、豆包/Seedream endpoint、尺寸和安全限制统一配置在根目录 `config.toml` 的 `[cover_generation]` 段落中。

## 构建

在项目根目录运行：

```powershell
.\scripts\build_docker.ps1 -ImageName automatic-editing -Tag latest -SaveTar
```

脚本会：

- Docker build context 只包含 `docker/`、`models/`、`tts_ref/`。
- 把 `models/` 打包到镜像内的 `/opt/automatic-editing/models`。
- 把 `tts_ref/` 打包到镜像内的 `/opt/automatic-editing/tts_ref`。
- 导出 `comfy_5090_313_auto` 的环境快照到 `docker/build-info/`，方便排查版本。
- 构建镜像 `automatic-editing:latest`。
- 如果传入 `-SaveTar`，会额外生成 `automatic-editing-latest.tar`，可复制到离线机器。

## 运行

```powershell
.\scripts\run_docker.ps1 -ImageName automatic-editing -Tag latest
```

或直接运行：

```powershell
docker run --rm --gpus all -p 7860:7860 --name automatic-editing `
  -v "E:\ai\skills\AutomaticEditing:/workspace" `
  -v "E:\ai\skills\AutomaticEditing\videos:/workspace/videos" `
  -v "E:\ai\skills\AutomaticEditing\outputs:/workspace/outputs" `
  automatic-editing:latest
```

打开：

```text
http://目标机器IP:7860
```

## 离线部署

在构建机器：

```powershell
.\scripts\build_docker.ps1 -ImageName automatic-editing -Tag latest -SaveTar
```

把 `automatic-editing-latest.tar` 复制到目标机器后：

```bash
docker load -i automatic-editing-latest.tar
docker run --rm --gpus all -p 7860:7860 --name automatic-editing \
  -v /data/AutomaticEditing:/workspace \
  -v /data/automatic-editing-videos:/workspace/videos \
  -v /data/automatic-editing-outputs:/workspace/outputs \
  automatic-editing:latest
```

## 注意

原始环境 `comfy_5090_313_auto` 是 Windows Python 3.13 环境，其中包含 Windows 专用 wheel 和本地 `C:\Users\...\Downloads\*.whl` 依赖，不能原样复制到 Linux Docker 里运行。因此 Dockerfile 使用 Linux 可安装的运行依赖集合，并把原始 conda/pip 快照写入 `docker/build-info/` 作为版本记录。

容器启动时会检查 `/workspace` 是否包含 `web_app.py` 以及项目内的 Seedream Skill。如果挂载的代码目录没有 `models/` 或 `tts_ref/`，入口脚本会自动把镜像内置的模型目录软链接到 `/workspace/models` 和 `/workspace/tts_ref`。

如果目标机器不使用 GPU，可以运行：

```powershell
.\scripts\run_docker.ps1 -NoGpu
```
