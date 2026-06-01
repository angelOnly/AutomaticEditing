import socket

import uvicorn


def find_free_port(host: str = "127.0.0.1", start: int = 7860, limit: int = 20) -> int:
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"没有找到可用端口: {start}-{start + limit - 1}")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = find_free_port(host=host, start=7860)
    print(f"Web 工作台地址: http://{host}:{port}")
    uvicorn.run("web_app:app", host=host, port=port, reload=False)
