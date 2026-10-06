"""Small PIN-gated web client, bounded sessions and private settings persistence."""

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections import OrderedDict
from dataclasses import fields
from html import escape
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agentcall.api.auth import authorized
from agentcall.service.config import Config
from agentcall.tasks.models import DEFAULT_BACKGROUND

COOKIE = "agentcall_session"
WEB = Path(__file__).with_name("web")
READ_ONLY = {"pin_auth", "token_env"}
LIVE = {
    "provider",
    "api_key_env",
    "gemini_api_key_env",
    "model_connect_seconds",
    "answer_timeout_seconds",
    "audio_timeout_seconds",
    "hangup_timeout_seconds",
}


def private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".agentcall-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def settings_dict(config):
    return {
        "service": {
            f.name: getattr(config, f.name) for f in fields(Config) if f.name != "provider"
        },
        "provider": config.provider.model_dump(mode="json"),
    }


class Sessions:
    def __init__(self, config, ttl=43200, maximum=128):
        self.config, self.ttl, self.maximum = config, ttl, maximum
        self.entries = OrderedDict()

    def fingerprint(self):
        return hashlib.sha256(self.config.token.encode()).digest()

    def create(self):
        if len(self.entries) >= self.maximum:
            self.entries.popitem(last=False)
        session = secrets.token_urlsafe(32)
        self.entries[session] = (monotonic() + self.ttl, self.fingerprint())
        return session

    def valid(self, connection):
        session = connection.cookies.get(COOKIE)
        entry = self.entries.get(session)
        if not entry:
            return False
        if entry[0] <= monotonic() or entry[1] != self.fingerprint():
            self.entries.pop(session, None)
            return False
        self.entries.move_to_end(session)
        return True

    def logout(self, request):
        self.entries.pop(request.cookies.get(COOKIE), None)


def same_origin(request):
    origin = request.headers.get("origin")
    scheme = {"ws": "http", "wss": "https"}.get(request.url.scheme, request.url.scheme)
    return origin is None or origin == f"{scheme}://{request.headers.get('host')}"


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str = Field(min_length=1, max_length=256)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openai: str | None = Field(default=None, min_length=1, max_length=4096)
    gemini: str | None = Field(default=None, min_length=1, max_length=4096)
    pin: str | None = Field(default=None, pattern=r"^[0-9]{4}$")


def install_ui(app, config, sessions, check_auth):
    router = APIRouter()

    @router.get("/ui", include_in_schema=False)
    async def ui(request: Request):
        authenticated = sessions.valid(request)
        page = (WEB / ("index.html" if authenticated else "login.html")).read_text()
        return HTMLResponse(
            page.replace("__API_BASE__", escape(config.root_path, quote=True)).replace(
                "__DEFAULT_BACKGROUND__", escape(DEFAULT_BACKGROUND)
            )
        )

    @router.get("/ui/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in {"app.js", "login.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @router.post("/session/login", tags=["web"])
    async def login(body: Login, request: Request):
        if not same_origin(request) or request.headers.get("X-AgentCall-CSRF") != "1":
            raise HTTPException(403, "Same-origin request required")
        # Reuse the API limiter; no PIN appears in URLs, logs or response bodies.
        status, retry = check_auth(request, credential=f"Bearer {body.pin}")
        if (
            status != 200
            or not config.token
            or not authorized(f"Bearer {body.pin}", config.token, config.pin_auth)
        ):
            return JSONResponse(
                {"detail": "Too many authentication failures" if retry else "Invalid PIN"},
                status_code=429 if retry else 401,
                headers={"Retry-After": str(retry)} if retry else {},
            )
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            COOKIE,
            sessions.create(),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=sessions.ttl,
            path="/",
        )
        return response

    @router.post("/session/logout", tags=["web"])
    async def logout(request: Request):
        sessions.logout(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    def credential_status():
        return {
            "openai": bool(os.environ.get(config.api_key_env)),
            "gemini": bool(os.environ.get(config.gemini_api_key_env)),
            "pin": bool(config.token),
        }

    @router.get("/settings", tags=["settings"])
    async def settings():
        active = settings_dict(config)
        path = getattr(config, "source", None)
        saved_path = Path(str(path) + ".ui.json") if path else None
        saved = json.loads(saved_path.read_text()) if saved_path and saved_path.exists() else active
        restart = [k for k, v in saved["service"].items() if active["service"][k] != v]
        return {
            "active": active,
            "saved": saved,
            "restart_required": restart,
            "read_only": sorted(READ_ONLY),
            "persistent": bool(path),
            "credentials": credential_status(),
        }

    @router.put("/settings", tags=["settings"])
    async def save_settings(body: dict):
        path = getattr(config, "source", None)
        if path is None:
            raise HTTPException(409, "Start the service with --config to persist settings")
        current = settings_dict(config)
        if set(body) != {"service", "provider"} or not isinstance(body["service"], dict):
            raise ValueError("settings require service and provider objects")
        if set(body["service"]) != set(current["service"]):
            raise ValueError("service fields must match GET /settings")
        if any(body["service"][key] != current["service"][key] for key in READ_ONLY):
            raise ValueError("PIN authentication and token environment cannot be changed here")
        try:
            candidate = (
                TypeAdapter(Config)
                .validate_python({**body["service"], "provider": body["provider"]})
                .validate()
            )
        except ValidationError:
            raise ValueError("Invalid settings: check field types and provider options") from None
        # Environment names, not credential values, are public settings.
        for name in (candidate.api_key_env, candidate.gemini_api_key_env):
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or name == config.token_env:
                raise ValueError("Invalid or reserved API key environment name")
        if candidate.api_key_env == candidate.gemini_api_key_env:
            raise ValueError("Providers require distinct credential environment names")
        private_write(
            Path(str(path) + ".ui.json"),
            json.dumps(settings_dict(candidate), ensure_ascii=False, allow_nan=False, indent=2)
            + "\n",
        )
        for name in LIVE:
            setattr(config, name, getattr(candidate, name))
        return await settings()

    @router.put("/settings/credentials", tags=["settings"])
    async def credentials(body: Credentials):
        path = getattr(config, "source", None)
        if path is None:
            raise HTTPException(409, "Start the service with --config to persist credentials")
        updates = {
            key: value
            for key, value in (
                (config.api_key_env, body.openai),
                (config.gemini_api_key_env, body.gemini),
            )
            if value is not None
        }
        if any(
            not value.isascii()
            or any(c.isspace() for c in value)
            or not all(c.isalnum() or c in "_-.:/+=@" for c in value)
            for value in updates.values()
        ):
            raise ValueError("API keys must use printable token characters without spaces")
        env_path = path.parent / "environment"
        if updates:
            # Keep unrelated systemd environment entries intact; never return them.
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            lines = [line for line in lines if line.partition("=")[0] not in updates]
            lines.extend(f"{key}={value}" for key, value in updates.items())
            private_write(env_path, "\n".join(lines) + "\n")
            os.environ.update(updates)
        if body.pin is not None:
            private_write(path.parent / "pin.env", f"{config.token_env}={body.pin}\n")
            os.environ[config.token_env] = body.pin
            sessions.entries.clear()
        return {"configured": credential_status(), "login_required": body.pin is not None}

    app.include_router(router)
