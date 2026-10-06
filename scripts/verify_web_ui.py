"""Exercise the web UI against a mock HFP phone; never contact a real phone or provider.

Install the optional browser-test extra and run playwright install chromium first.
AGENTCALL_TEST_BROWSER optionally selects an existing Chromium executable.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import DEVICE, Phone  # noqa: E402

from agentcall.api.app import create_app  # noqa: E402
from agentcall.bluetooth.hfp import HFPConnection  # noqa: E402
from agentcall.service.backend import Backend  # noqa: E402
from agentcall.service.config import Config  # noqa: E402
from agentcall.storage.store import Store  # noqa: E402


async def verify(directory):
    config_path = directory / "config.toml"
    config_path.write_text('[service]\npin_auth=true\nroot_path="/api"\n')
    os.environ["AGENTCALL_TOKEN"] = "123456"
    config = Config.load(config_path)
    backend = Backend(config, Store(":memory:"))
    host, peer = socket.socketpair()
    phone = Phone(peer)

    async def dbus_call(*args, **kwargs):
        return []

    async def devices():
        return [
            {
                "id": DEVICE.split("/")[-1],
                "path": DEVICE,
                "name": "Test Android",
                "address": "11:22:33:44:55:66",
                "connected": True,
            }
        ]

    async def close():
        pass

    async def start():
        backend.running = True

    backend.start = start
    backend.bluez = SimpleNamespace(
        call=dbus_call, devices=devices, close=close, agent=SimpleNamespace(pending={})
    )
    backend.ensure_audio = lambda device: None
    connection = HFPConnection(host, DEVICE, backend.emit, timeout=0.3)
    backend.connections[DEVICE] = connection
    await connection.start()
    backend.store.db.execute(
        "INSERT INTO contacts VALUES (?,?,?,?,?,?)",
        ("contact1", DEVICE, "Test Contact", "12345", "", "now"),
    )
    backend.store.db.commit()
    app = create_app(config, backend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=os.environ.get("AGENTCALL_TEST_BROWSER"),
                headless=True,
                args=["--no-sandbox"],
            )
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{port}/api/ui")
            await expect(page.locator("#login")).to_be_visible()
            await expect(page.locator("#settings-form")).to_have_count(0)
            await page.locator("#pin").fill("123456")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.locator("#device-list")).to_contain_text("Test Android")
            await page.locator('[data-tab="contacts"]').click()
            await page.locator("#contact-search button").first.click()
            await expect(page.locator("#contact-list")).to_contain_text("Test Contact")
            await page.get_by_role("button", name="用于任务").click()
            await expect(page.locator('#task-create [name="contact_id"]')).to_have_value("contact1")
            await page.locator('#task-create [name="goal"]').fill("Browser test only")
            await page.locator('#task-create [name="background"]').fill("Background")
            await page.locator('#task-create [name="information"]').fill('{"reference":"test"}')
            await page.locator('#task-create [name="completion_criteria"]').fill(
                "Return a test result"
            )
            await page.get_by_role("button", name="创建任务", exact=True).click()
            await expect(page.locator("#task-detail")).to_contain_text('"state": "saved"')
            await page.locator("#task-search button").click()
            await expect(page.locator("#task-list")).to_contain_text("12345")
            await page.locator('#task-control button[value="result"]').click()
            await expect(page.locator("#task-detail")).to_contain_text('"model_result"')
            await page.locator('[data-tab="settings"]').click()
            await expect(page.locator('[name="service_adapter"]')).to_have_value("hci0")
            await page.locator('#settings-form [name="provider"]').select_option("gemini")
            await expect(page.locator('#settings-form [name="voice"]')).to_have_value("Aoede")
            await page.locator('#settings-form [name="language"]').fill("English")
            await page.locator('#settings-form [name="options"]').fill('{"temperature":0.5}')
            await page.locator('#settings-form [name="service_answer_timeout_seconds"]').fill("90")
            await page.get_by_role("button", name="保存配置", exact=True).click()
            await expect(page.locator("#notice")).to_have_text("配置已保存")
            assert Config.load(config_path).provider.provider == "gemini"
            await page.locator('#credentials [name="openai"]').fill("sk-browser-test")
            await page.get_by_role("button", name="保存凭据", exact=True).click()
            await expect(page.locator("#credential-status")).to_contain_text("OpenAI：已配置")
            await expect(page.locator('#credentials [name="openai"]')).to_have_value("")
            await page.locator('[data-tab="events"]').click()
            await page.locator("#watch").click()
            await expect(page.locator("#event-output")).to_contain_text("events.ready")
            backend.emit("verification.probe", value=42)
            await expect(page.locator("#event-output")).to_contain_text("verification.probe")
            await page.locator("#stop-watch").click()
            await page.locator('[data-tab="calls"]').click()
            await page.locator('#dial [name="number"]').fill("12345")
            await page.locator("#dial button").first.click()
            await expect(page.locator("#current-list")).to_contain_text("12345")
            await phone.send("\r\n+CIEV: 1,1\r\n+CIEV: 2,0\r\n")
            for _ in range(100):
                if connection.state == "active":
                    break
                await asyncio.sleep(0.01)
            assert connection.state == "active"
            await page.locator('#call-control [name="digits"]').fill("12#")
            await page.locator('#call-control button[value="dtmf"]').click()
            await expect(page.locator("#notice")).to_have_text("请求已提交")
            assert any(cmd.startswith("ATD") for cmd in phone.commands)
            assert "AT+VTS=#" in phone.commands
            await page.locator('#call-control button[value="hangup"]').click()
            await expect(page.locator("#notice")).to_have_text("请求已提交")
            await page.locator('[data-tab="settings"]').click()
            await page.locator('#credentials [name="pin"]').fill("987654")
            await page.locator('#credentials [name="confirm"]').fill("987654")
            await page.get_by_role("button", name="保存凭据", exact=True).click()
            await expect(page.locator("#login")).to_be_visible()
            await page.locator("#pin").fill("987654")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.locator("#device-list")).to_contain_text("Test Android")
            await page.locator("#logout").click()
            await expect(page.locator("#login")).to_be_visible()
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "browser": "Chromium",
                        "mock_phone": True,
                        "login": True,
                        "contacts": True,
                        "task_create": True,
                        "task_result": True,
                        "settings_save_reload": True,
                        "credentials_write_only": True,
                        "sse": True,
                        "dial_dtmf_hangup": True,
                        "pin_rotation": True,
                        "logout": True,
                        "javascript_errors": len(errors),
                    }
                )
            )
            await browser.close()
    finally:
        server.should_exit = True
        await serving
        await phone.close()
        backend.store.close()


async def main():
    with tempfile.TemporaryDirectory(prefix="agentcall-ui-test-") as directory:
        await verify(Path(directory))


if __name__ == "__main__":
    asyncio.run(main())
