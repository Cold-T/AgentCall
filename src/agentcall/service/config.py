import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentcall.tasks.models import ProviderConfig


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    root_path: str = ""
    adapter: str = "hci0"
    codec: str = "cvsd"
    obex_bus: str = "session"
    database: str = "~/.local/share/agentcall/agentcall.sqlite3"
    token_env: str = "AGENTCALL_TOKEN"
    reconnect_seconds: float = 5
    api_key_env: str = "OPENAI_API_KEY"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    model_connect_seconds: float = 20
    answer_timeout_seconds: float = 60
    audio_timeout_seconds: float = 10
    hangup_timeout_seconds: float = 5
    provider: ProviderConfig = field(default_factory=ProviderConfig)

    @property
    def token(self):
        return os.environ.get(self.token_env, "")

    @classmethod
    def load(cls, path=None):
        data = tomllib.loads(Path(path).read_text()) if path else {}
        config = cls(**data.get("service", {}), provider=ProviderConfig(**data.get("provider", {})))
        if config.codec not in ("cvsd", "msbc"):
            raise ValueError("codec must be cvsd or msbc")
        if config.obex_bus not in ("session", "system"):
            raise ValueError("obex_bus must be session or system")
        if config.root_path and (
            not config.root_path.startswith("/")
            or config.root_path.endswith("/")
            or "//" in config.root_path
            or "?" in config.root_path
            or "#" in config.root_path
        ):
            raise ValueError("root_path must be empty or an absolute path without a trailing slash")
        if config.host not in ("127.0.0.1", "localhost", "::1") and not config.token:
            raise ValueError("non-loopback binding requires a Bearer token environment variable")
        for name in (
            "reconnect_seconds",
            "model_connect_seconds",
            "answer_timeout_seconds",
            "audio_timeout_seconds",
            "hangup_timeout_seconds",
        ):
            if getattr(config, name) <= 0:
                raise ValueError(f"{name} must be positive")
        return config
