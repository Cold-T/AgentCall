import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    adapter: str = "hci0"
    codec: str = "cvsd"
    database: str = "~/.local/share/agentcall/agentcall.sqlite3"
    token_env: str = "AGENTCALL_TOKEN"
    reconnect_seconds: float = 5

    @property
    def token(self):
        return os.environ.get(self.token_env, "")

    @classmethod
    def load(cls, path=None):
        data = tomllib.loads(Path(path).read_text()) if path else {}
        config = cls(**data.get("service", {}))
        if config.codec not in ("cvsd", "msbc"):
            raise ValueError("codec must be cvsd or msbc")
        if config.host not in ("127.0.0.1", "localhost", "::1") and not config.token:
            raise ValueError("non-loopback binding requires a Bearer token environment variable")
        if config.reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be positive")
        return config
