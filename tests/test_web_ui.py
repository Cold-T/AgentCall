import copy
import os
from types import SimpleNamespace

import httpx
import pytest

from agentcall.api import ui
from agentcall.api.app import create_app
from agentcall.service.config import Config
from agentcall.tasks.manager import TaskManager
from agentcall.tasks.models import DEFAULT_BACKGROUND, TaskInput

CSRF = {"X-AgentCall-CSRF": "1", "Origin": "https://test"}


def web_app(service, monkeypatch, tmp_path):
    backend = service[0]
    monkeypatch.setenv("AGENTCALL_TOKEN", "0123")
    path = tmp_path / "config.toml"
    path.write_text('[service]\npin_auth=true\nroot_path="/api"\n')
    config = Config.load(path)
    backend.config = config
    return create_app(config, backend), config


async def login(client, pin="0123"):
    return await client.post("/api/session/login", json={"pin": pin}, headers=CSRF)


async def test_ui_gate_session_cookie_logout_and_bearer(service, monkeypatch, tmp_path):
    app, _ = web_app(service, monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        page = await c.get("/api/ui")
        assert 'id="login"' in page.text and 'id="task-create"' not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.get("/api/ui/assets/app.js")).status_code == 200
        assert (await c.get("/api/ui/assets/pin.env")).status_code == 404
        wrong = await login(c, "6543")
        assert wrong.status_code == 401 and "6543" not in wrong.text
        response = await login(c)
        assert response.status_code == 200 and "0123" not in response.text
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie.lower() for flag in ("secure", "httponly", "samesite=strict"))
        assert "0123" not in cookie
        assert 'id="task-create"' in (await c.get("/api/ui")).text
        assert (await c.get("/api/devices")).status_code == 200
        assert (await c.get("/api/docs")).status_code == 200
        assert (await c.post("/api/session/logout", headers=CSRF)).status_code == 200
        assert (await c.get("/api/devices")).status_code == 401
        assert 'id="login"' in (await c.get("/api/ui")).text
        assert (
            await c.get("/api/health", headers={"Authorization": "Bearer 0123"})
        ).status_code == 200


async def test_cookie_csrf_protects_actions_and_login(service, monkeypatch, tmp_path):
    app, _ = web_app(service, monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        assert (await c.post("/api/session/login", json={"pin": "0123"})).status_code == 403
        evil = {**CSRF, "Origin": "https://evil.test"}
        assert (
            await c.post("/api/session/login", json={"pin": "0123"}, headers=evil)
        ).status_code == 403
        assert (await login(c)).status_code == 200
        for headers in ({}, evil):
            assert (await c.post("/api/calls", json={}, headers=headers)).status_code == 403
        assert (await c.post("/api/discovery/start", headers=CSRF)).status_code == 200
        assert not any(cmd.startswith("ATD") for cmd in service[2].commands)


async def test_login_rate_limit_shared_with_api(service, monkeypatch, tmp_path):
    app, _ = web_app(service, monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        for _ in range(10):
            assert (await login(c, "6543")).status_code == 401
        response = await login(c)
        assert response.status_code == 429 and "retry-after" in response.headers
        assert (
            await c.get("/api/health", headers={"Authorization": "Bearer 0123"})
        ).status_code == 429
        assert 'id="login"' in (await c.get("/api/ui")).text


async def test_private_pin_file_is_authoritative_and_updates_live(service, monkeypatch, tmp_path):
    app, config = web_app(service, monkeypatch, tmp_path)
    path = tmp_path / "pin.env"
    ui.private_write(path, "AGENTCALL_TOKEN=9876\n")
    assert config.token == "9876"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        assert (await login(c)).status_code == 401
        assert (await login(c, "9876")).status_code == 200
        ui.private_write(path, "AGENTCALL_TOKEN=4567\n")
        expired = await c.get("/api/health", headers=CSRF)
        assert expired.status_code == 401
        assert expired.headers["www-authenticate"] == "Bearer"
        assert (await login(c, "9876")).status_code == 401
        assert (await login(c, "4567")).status_code == 200
        ui.private_write(path, "AGENTCALL_TOKEN=invalid\n")
        assert (await c.get("/api/health")).status_code == 401
        assert (await login(c)).status_code == 401


async def test_history_links_calls_once_includes_failed_tasks_and_paginates(
    service, monkeypatch, tmp_path
):
    app, config = web_app(service, monkeypatch, tmp_path)
    backend = service[0]
    store = backend.store
    manager = TaskManager(backend, config)
    device = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    task = manager.create(TaskInput(device=device, number="12345", goal="test goal"))
    linked = store.new_call(device, "12345", "outgoing", "ended")
    store.update_task(task["id"], call_id=linked, state="ended", outcome="completed")
    failed = manager.create(TaskInput(device=device, number="54321", goal="failed task"))
    store.update_task(failed["id"], state="ended", outcome="incomplete", error={"message": "test"})
    manual = store.new_call(device, "55555", "outgoing", "ended")
    store.save_phonebook(
        device,
        None,
        [{"id": "pbap1", "number": "11111", "synced_at": "2026-10-06T00:00:00+00:00"}],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        assert (await c.get("/api/history")).status_code == 401
        await login(c)
        rows = (await c.get("/api/history")).json()
        assert len(rows) == 4
        assert sum(row["call"] is not None and row["call"]["id"] == linked for row in rows) == 1
        assert {row["id"] for row in rows} == {task["id"], failed["id"], manual, "pbap1"}
        assert rows == sorted(rows, key=lambda row: row["recorded_at"], reverse=True)
        first = (await c.get("/api/history?limit=2")).json()
        second = (await c.get("/api/history?limit=2&offset=2")).json()
        assert first + second == rows
        assert (await c.get("/api/history?device=dev_22_22_33_44_55_66")).json() == []


async def test_settings_persist_reload_and_apply_defaults(service, monkeypatch, tmp_path):
    app, config = web_app(service, monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        await login(c)
        data = (await c.get("/api/settings")).json()
        body = copy.deepcopy(data["saved"])
        body["provider"] = {
            "provider": "gemini",
            "language": "English",
            "options": {"temperature": 0.5},
        }
        body["service"]["adapter"] = "hci1"
        body["service"]["answer_timeout_seconds"] = 90
        response = await c.put("/api/settings", json=body, headers=CSRF)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["restart_required"] == ["adapter"]
        assert config.adapter == "hci0" and config.answer_timeout_seconds == 90
        assert config.provider.provider == "gemini"
        override = tmp_path / "config.toml.ui.json"
        assert override.stat().st_mode & 0o777 == 0o600
        loaded = Config.load(config.source)
        assert loaded.adapter == "hci1" and loaded.provider.language == "English"
        task = await c.post(
            "/api/tasks",
            headers=CSRF,
            json={
                "device": "dev_11_22_33_44_55_66",
                "number": "12345",
                "goal": "test task",
            },
        )
        assert task.status_code == 201
        assert task.json()["config"]["provider"] == "gemini"
        assert task.json()["input"]["background"] == DEFAULT_BACKGROUND
        assert task.json()["state"] == "saved"
        assert not any(cmd.startswith("ATD") for cmd in service[2].commands)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pin_auth", False),
        ("token_env", "OTHER"),
        ("port", 0),
        ("codec", "bad"),
        ("audio_timeout_seconds", -1),
        ("root_path", "bad"),
        ("api_key_env", "AGENTCALL_TOKEN"),
        ("api_key_env", "invalid-name"),
        ("gemini_api_key_env", "OPENAI_API_KEY"),
    ],
)
async def test_invalid_settings_never_persist(service, monkeypatch, tmp_path, field, value):
    app, _ = web_app(service, monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        await login(c)
        body = (await c.get("/api/settings")).json()["saved"]
        body["service"][field] = value
        assert (await c.put("/api/settings", json=body, headers=CSRF)).status_code == 400
        assert not (tmp_path / "config.toml.ui.json").exists()


async def test_credentials_write_only_validation_and_pin_rotation(service, monkeypatch, tmp_path):
    app, _ = web_app(service, monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "before")
    monkeypatch.setenv("GEMINI_API_KEY", "before")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        await login(c)
        for body in (
            {"pin": "bad-secret"},
            {"pin": "123"},
            {"pin": "12345"},
            {"pin": "123456"},
            {"pin": "１２３４"},
            {"pin": "123\n"},
            {"openai": "secret\nINJECTED=true"},
        ):
            response = await c.put("/api/settings/credentials", json=body, headers=CSRF)
            assert response.status_code in (400, 422)
            assert "secret" not in response.text
        response = await c.put(
            "/api/settings/credentials",
            headers=CSRF,
            json={"openai": "sk-test-key", "gemini": "gemini-test-key"},
        )
        assert response.status_code == 200
        assert "test-key" not in response.text
        assert os.environ["OPENAI_API_KEY"] == "sk-test-key"
        env = tmp_path / "environment"
        assert env.stat().st_mode & 0o777 == 0o600
        assert "sk-test-key" in env.read_text()
        settings = await c.get("/api/settings")
        assert "test-key" not in settings.text and "0123" not in settings.text
        assert settings.json()["credentials"] == {"openai": True, "gemini": True, "pin": True}
        second = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")
        async with second:
            await login(second)
            response = await c.put("/api/settings/credentials", headers=CSRF, json={"pin": "9876"})
            assert response.status_code == 200 and response.json()["login_required"]
            assert "9876" not in response.text
            assert (tmp_path / "pin.env").stat().st_mode & 0o777 == 0o600
            assert (await c.get("/api/devices")).status_code == 401
            assert (await second.get("/api/devices")).status_code == 401
            assert (await login(c)).status_code == 401
            assert (await login(c, "9876")).status_code == 200


def test_sessions_bounded_expire_and_reject_forgery(monkeypatch):
    monkeypatch.setenv("AGENTCALL_TOKEN", "0123")
    now = [0]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    sessions = ui.Sessions(Config(), ttl=10, maximum=2)
    one, two, three = (sessions.create() for _ in range(3))

    def request(token):
        return SimpleNamespace(cookies={ui.COOKIE: token})

    assert len(sessions.entries) == 2 and not sessions.valid(request(one))
    assert not sessions.valid(request("forged"))
    assert sessions.valid(request(two)) and sessions.valid(request(three))
    now[0] = 10
    assert not sessions.valid(request(two))
    token = sessions.create()
    monkeypatch.setenv("AGENTCALL_TOKEN", "6543")
    assert not sessions.valid(request(token))


def test_ws_origin_scheme():
    assert ui.same_origin(
        SimpleNamespace(
            url=SimpleNamespace(scheme="wss"), headers={"host": "test", "origin": "https://test"}
        )
    )
    assert not ui.same_origin(
        SimpleNamespace(
            url=SimpleNamespace(scheme="wss"),
            headers={"host": "test", "origin": "https://evil.test"},
        )
    )


def test_nonfinite_timeouts_rejected():
    with pytest.raises(ValueError, match="positive"):
        Config(answer_timeout_seconds=float("nan")).validate()
