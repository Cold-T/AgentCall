import importlib.util
from pathlib import Path

import httpx
import pytest

from agentcall.api import auth
from agentcall.api.app import create_app
from agentcall.service.config import Config

spec = importlib.util.spec_from_file_location(
    "set_pin", Path(__file__).parents[1] / "scripts/set_pin.py"
)
set_pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_pin)


async def test_pin_bearer_basic_failure_limit_and_client_isolation(service, monkeypatch):
    backend, _, _, _, _ = service
    backend.config.pin_auth = True
    monkeypatch.setenv("AGENTCALL_TOKEN", "123456")
    now = [100.0]
    monkeypatch.setattr(auth, "monotonic", lambda: now[0])
    app = create_app(backend.config, backend)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.1", 123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health", auth=("pin", "123456"))).status_code == 200
        good = {"Authorization": "Bearer 123456"}
        assert (await client.get("/health", headers=good)).status_code == 200
        schema = (await client.get("/openapi.json", headers=good)).json()
        assert schema["components"]["securitySchemes"]["PinBasic"]["scheme"] == "basic"
        assert schema["paths"]["/health"]["get"]["security"] == [
            {"BearerAuth": []},
            {"PinBasic": []},
        ]
        for _ in range(10):
            response = await client.get("/devices", headers={"Authorization": "Basic !!!"})
            assert response.status_code == 401
            assert response.headers["www-authenticate"].startswith("Basic ")
            assert "123456" not in response.text
        blocked = await client.get("/health", headers=good)
        assert blocked.status_code == 429 and blocked.headers["retry-after"] == "60"
        other = httpx.ASGITransport(app=app, client=("192.0.2.2", 123))
        async with httpx.AsyncClient(transport=other, base_url="http://test") as second:
            assert (await second.get("/health", headers=good)).status_code == 200
        now[0] += 60
        assert (await client.get("/health", headers=good)).status_code == 200


def test_pin_mode_cannot_start_without_pin(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCALL_TOKEN", raising=False)
    path = tmp_path / "config.toml"
    path.write_text("[service]\npin_auth=true\n")
    with pytest.raises(ValueError, match="PIN"):
        Config.load(path)
    with pytest.raises(ValueError, match="PIN"):
        create_app(Config(pin_auth=True))
    monkeypatch.setenv("AGENTCALL_TOKEN", "not-a-pin")
    with pytest.raises(ValueError, match="PIN"):
        Config.load(path)
    monkeypatch.setenv("AGENTCALL_TOKEN", "123456")
    assert Config.load(path).pin_auth


def test_pin_file_private_and_not_echoed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(set_pin.getpass, "getpass", lambda prompt: "123456")
    set_pin.main()
    path = tmp_path / ".config/agentcall/pin.env"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == "AGENTCALL_TOKEN=123456\n"
    assert "123456" not in capsys.readouterr().out


def test_limiter_observations_bounded():
    limiter = auth.FailedAuthLimiter(clients=2)
    for client in ("one", "two", "three"):
        limiter.failed(client)
    assert len(limiter.failures) == 2
