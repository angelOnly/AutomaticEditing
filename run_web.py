import socket
import sys

import uvicorn

from newsclip_agent.config import load_config
from newsclip_agent.asr_preflight import run_asr_preflight


def find_free_port(host: str = "127.0.0.1", start: int = 7860, limit: int = 20) -> int:
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"没有找到可用端口: {start}-{start + limit - 1}")


def preflight() -> None:
    config = load_config("config.toml")
    funasr_cfg = config.funasr
    if not bool(funasr_cfg.get("startup_check", True)):
        print("跳过启动前 ASR 环境自检")
        return

    print("启动前检查：FFmpeg / CUDA / FunASR / 模型路径 ...")
    report = run_asr_preflight(
        config,
        smoke_test=bool(funasr_cfg.get("startup_smoke_test", False)),
    )

    if not report.get("ok"):
        print("启动前检查失败：")
        print(report)
        raise SystemExit(2)

    print("启动前检查通过")


if __name__ == "__main__":
    if "--no-preflight" not in sys.argv:
        preflight()
    else:
        sys.argv.remove("--no-preflight")

    host = "127.0.0.1"
    port = find_free_port(host=host, start=7860)
    print(f"Web 工作台地址: http://{host}:{port}")
    uvicorn.run("web_app:app", host=host, port=port, reload=False)
