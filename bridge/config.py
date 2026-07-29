"""配置管理

使用 pydantic-settings 从环境变量和 .env 文件读取配置。
"""

import logging

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置集合

    所有配置项都支持通过环境变量覆盖。如果存在 .env 文件，会自动读取。
    例如: LIGHTRAG_API_URL=http://localhost:9621
    """

    # ── LightRAG 连接配置 ──────────────────────────────────────────
    lightrag_api_url: str = Field(
        default="http://localhost:9621",
        description="LightRAG API 的基础 URL。如果 LightRAG 部署在其他主机，修改此值。",
    )

    # ── Outline Webhook 安全配置 ───────────────────────────────────
    outline_webhook_secret: str = Field(
        default="",
        description="Outline Webhook 的共享密钥，用于 HMAC-SHA256 签名验证。"
                    "在 Outline 管理后台配置 Webhook 时设置。为空时跳过验证（仅测试用）。",
    )

    # ── 桥接服务监听配置 ───────────────────────────────────────────
    bridge_host: str = Field(default="0.0.0.0", description="桥接服务监听地址。0.0.0.0 表示监听所有网络接口。")
    bridge_port: int = Field(default=9641, description="桥接服务监听端口。Outline 配置 Webhook 时需指向此端口。")

    # ── LightRAG 异步处理轮询配置 ──────────────────────────────────
    poll_interval: int = Field(
        default=2, description="轮询 LightRAG 处理状态的时间间隔（秒）。"
    )
    poll_max_attempts: int = Field(
        default=60, description="最大轮询次数。与 poll_interval 共同决定最长等待时间（默认 120 秒）。"
    )

    # ── LightRAG 删除重试配置 ──────────────────────────────────────
    delete_retry_attempts: int = Field(
        default=2, description="LightRAG pipeline busy 时的最大重试次数。"
    )
    delete_retry_delay: int = Field(
        default=3, description="每次重试之间的等待时间（秒）。"
    )

    # ── 日志配置 ───────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="日志级别：DEBUG, INFO, WARNING, ERROR")

    # ── 数据库配置 ──────────────────────────────────────────────────
    db_path: str = Field(
        default="bridge.db",
        description="SQLite 数据库文件路径。Docker 环境下建议设为 /app/data/bridge.db",
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# 全局配置单例
settings = Settings()

# 日志配置
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("outline-rag-bridge")
