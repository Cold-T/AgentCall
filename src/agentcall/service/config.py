import json
import math
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
    pin_auth: bool = False
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
        source = getattr(self, "source", None)
        if self.pin_auth and source:
            path = source.parent / "pin.env"
            if path.exists():
                try:
                    values = [
                        line.partition("=")[2]
                        for line in path.read_text().splitlines()
                        if line.partition("=")[0] == self.token_env
                    ]
                except (OSError, UnicodeError):
                    return ""
                if len(values) != 1:
                    return ""
                pin = values[0]
                return pin if pin.isascii() and pin.isdigit() and len(pin) == 4 else ""
        return os.environ.get(self.token_env, "")

    @classmethod
    def load(cls, path=None):
        data = tomllib.loads(Path(path).read_text()) if path else {}
        if path:
            overrides = Path(str(path) + ".ui.json")
            if overrides.exists():
                data = json.loads(overrides.read_text())
        config = cls(**data.get("service", {}), provider=ProviderConfig(**data.get("provider", {})))
        config.source = Path(path).expanduser().resolve() if path else None
        return config.validate()

    def validate(self):
        config = self
        if not isinstance(config.port, int) or not 1 <= config.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if config.codec not in ("cvsd", "msbc"):
            raise ValueError("codec must be cvsd or msbc")
        if config.obex_bus not in ("session", "system"):
            raise ValueError("obex_bus must be session or system")
        if config.pin_auth and (
            not config.token.isascii() or not config.token.isdigit() or len(config.token) != 4
        ):
            raise ValueError("pin_auth requires a configured 4 digit PIN in token_env")
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
            if not math.isfinite(getattr(config, name)) or getattr(config, name) <= 0:
                raise ValueError(f"{name} must be positive")
        return config
