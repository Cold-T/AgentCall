import httpx
import pytest
from typer.testing import CliRunner

from agentcall.api.app import create_app
from agentcall.cli.main import app as cli
from agentcall.service.config import Config


async def test_gateway_prefix_docs_and_bearer_auth(service, monkeypatch):
    backend, _, _, _, _ = service
    backend.config.root_path = "/api"
    monkeypatch.setenv("AGENTCALL_TOKEN", "gateway-test-token")
    app = create_app(backend.config, backend)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/api/health")).status_code == 401
        headers = {"Authorization": "Bearer gateway-test-token"}
        for path in ("/health", "/api/health"):
            assert (await client.get(path, headers=headers)).json()["ready"]
        docs = (await client.get("/docs", headers=headers)).text
        assert "url: '/api/openapi.json'" in docs
        schema = (await client.get("/openapi.json", headers=headers)).json()
        assert {"url": "/api"} in schema["servers"]


@pytest.mark.parametrize("path", ["api", "/", "/api/", "/api//v1", "/api?q=1", "/api#x"])
def test_invalid_gateway_prefix(tmp_path, path):
    config = tmp_path / "config.toml"
    config.write_text(f'[service]\nroot_path="{path}"\n')
    with pytest.raises(ValueError, match="root_path"):
        Config.load(config)


def test_cli_sends_access_and_bearer_credentials(monkeypatch):
    monkeypatch.setenv("AGENTCALL_TOKEN", "origin-token")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "access-client")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "access-secret")
    requests = []

    def request(method, url, **kwargs):
        req = httpx.Request(method, url, headers=kwargs["headers"])
        requests.append(req)
        return httpx.Response(200, json={"ready": True}, request=req)

    monkeypatch.setattr(httpx, "request", request)
    result = CliRunner().invoke(
        cli, ["--url", "https://agentcall.coldt.uk/api", "--json", "health"]
    )
    assert result.exit_code == 0
    assert str(requests[0].url) == "https://agentcall.coldt.uk/api/health"
    assert requests[0].headers["CF-Access-Client-Id"] == "access-client"
    assert requests[0].headers["CF-Access-Client-Secret"] == "access-secret"
    assert requests[0].headers["Authorization"] == "Bearer origin-token"
    assert "access-secret" not in result.output and "origin-token" not in result.output


def test_partial_access_credentials_fail_before_http_request(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "access-client")
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)

    def request(*args, **kwargs):
        pytest.fail("must not send a request with partial credentials")

    monkeypatch.setattr(httpx, "request", request)
    result = CliRunner().invoke(cli, ["--json", "health"])
    assert result.exit_code == 1
    assert "configured together" in result.stderr
