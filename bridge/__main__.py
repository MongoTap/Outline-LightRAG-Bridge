"""入口点 — `python -m bridge` 启动服务。"""

from bridge import app
from bridge.config import settings


def main():
    import uvicorn

    uvicorn.run(
        app,
        host=settings.bridge_host,
        port=settings.bridge_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
