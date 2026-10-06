"""Verify the three-tab UI against a mock phone, without dialing or model API calls."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import DEVICE, Phone  # noqa: E402

from agentcall.api.app import create_app  # noqa: E402
from agentcall.api.ui import private_write  # noqa: E402
from agentcall.audio.recording import CallRecording  # noqa: E402
from agentcall.bluetooth.hfp import HFPConnection  # noqa: E402
from agentcall.service.backend import Backend  # noqa: E402
from agentcall.service.config import Config  # noqa: E402
from agentcall.storage.store import Store  # noqa: E402
from agentcall.tasks.manager import TaskManager  # noqa: E402
from agentcall.tasks.models import TaskInput  # noqa: E402


async def verify(directory):
    config_path = directory / "config.toml"
    config_path.write_text('[service]\npin_auth=true\nroot_path="/api"\n')
    os.environ["AGENTCALL_TOKEN"] = "1111"  # Deliberately stale startup credential.
    os.environ["OPENAI_API_KEY"] = "test-only"
    os.environ["GEMINI_API_KEY"] = "test-only"
    private_write(directory / "pin.env", "AGENTCALL_TOKEN=0123\n")
    config = Config.load(config_path)
    backend = Backend(config, Store(":memory:"))
    host, peer = socket.socketpair()
    phone = Phone(peer)

    async def nothing(*args, **kwargs):
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

    async def start():
        backend.running = True

    backend.start = start
    backend.bluez = SimpleNamespace(
        call=nothing, devices=devices, close=nothing, agent=SimpleNamespace(pending={})
    )
    backend.ensure_audio = lambda device: None
    connection = HFPConnection(host, DEVICE, backend.emit, timeout=0.3)
    backend.connections[DEVICE] = connection
    await connection.start()
    backend.store.save_phonebook(
        DEVICE, [{"id": "contact1", "name": "Test Contact", "number": "12345", "raw": ""}], []
    )
    manager = TaskManager(backend, config)

    def summary_response(request):
        assert json.loads(request.content)["model"] == "gpt-5.6-luna"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "已确认答案为 42。<img src=x onerror=alert(1)>",
                            }
                        ],
                    }
                ],
            },
        )

    manager.summaries.transport = httpx.MockTransport(summary_response)
    manager.start = nothing  # Keep submitted tasks queued; never run a model or dial.
    history_task = manager.create(TaskInput(device=DEVICE, number="22222", goal="History goal"))
    call_id = backend.store.new_call(DEVICE, "22222", "outgoing", "ended")
    backend.store.update_task(
        history_task["id"],
        call_id=call_id,
        state="ended",
        outcome="completed",
        model_result={"status": "completed", "result": {"answer": "42"}},
    )
    backend.store.task_event(
        history_task["id"],
        "model.transcript",
        {"role": "assistant", "text": "Hello from the assistant."},
    )
    for _ in range(1000):
        backend.store.task_event(history_task["id"], "model.transcript", {"text": ""})
    backend.store.task_event(
        history_task["id"],
        "model.transcript",
        {"role": "user", "text": "The answer is 42. <img src=x onerror=alert(1)>"},
    )
    backend.store.recordings_dir = directory / "recordings"
    recording = CallRecording(backend.store.recordings_dir / (history_task["id"] + ".wav"), 8000)
    recording.write(0, bytes(1600))
    recording.write(1, bytes(1600))
    assert recording.finish()
    app = create_app(config, backend, manager)
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
            await page.locator("#pin").fill("1111")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.locator("#error")).to_contain_text("PIN 不正确")
            await page.locator("#pin").fill("0123")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.locator("#device-list")).to_contain_text("Test Android")
            await expect(page.get_by_role("tab")).to_have_count(3)
            await page.get_by_role("tab", name="发起通话").click()
            await expect(page.locator('#task-create [name="contact_id"] option')).to_have_count(2)
            await expect(page.locator("#default-background")).to_contain_text("不要朗读任务说明")
            await expect(page.locator('#task-create [name="voice"]')).to_have_value("marin")
            await expect(page.locator('#task-create [name="voice"] option')).to_have_count(10)
            await page.locator('#task-create [name="voice"]').select_option("cedar")
            await page.locator('#task-create [name="model"]').select_option("1")
            await expect(page.locator('#task-create [name="voice"]')).to_have_value("Aoede")
            await page.locator('#task-create [name="voice"]').select_option("Puck")
            await page.locator('#task-create [name="model"]').select_option("0")
            await expect(page.locator('#task-create [name="voice"]')).to_have_value("cedar")
            await page.locator('#task-create [name="contact_id"]').select_option("contact1")
            await page.locator('#task-create [name="goal"]').fill("Browser contact goal")
            await page.locator('#task-create [name="language"]').select_option("English")
            await page.locator('#task-create [name="max_call_seconds"]').fill("120")
            await page.locator("#start-call").click()
            await expect(page.locator("#task-status")).to_contain_text("排队中")
            tasks = [backend.store.task(i) for i in backend.store.task_ids()]
            created = next(t for t in tasks if t["input"]["goal"] == "Browser contact goal")
            assert created["input"]["number"] == "12345"
            assert created["input"]["completion_criteria"] == "Browser contact goal"
            assert created["input"]["max_call_seconds"] == 120
            assert created["config"]["language"] == "English"
            assert created["config"]["voice"] == "cedar"
            assert created["config"]["options"]["transcription"]
            await page.locator('#task-create [name="number"]').fill("33333")
            await expect(page.locator('#task-create [name="contact_id"]')).to_have_value("")
            await page.locator('#task-create [name="goal"]').fill("Browser number goal")
            await page.locator("#start-call").click()
            await expect(page.locator('#task-create [name="goal"]')).to_have_value("")
            tasks = [backend.store.task(i) for i in backend.store.task_ids()]
            assert any(t["input"]["number"] == "33333" for t in tasks)
            assert not any(command.startswith("ATD") for command in phone.commands)
            await (
                page.locator("#task-status").get_by_role("button", name="查看", exact=True).click()
            )
            await expect(page.locator("#history-detail")).to_be_focused()
            await expect(page.locator("#tab-history")).to_have_attribute("aria-selected", "true")
            await page.get_by_role("tab", name="查看历史").click()
            row = page.locator("#history-list tr").filter(has_text="History goal")
            await row.get_by_role("button", name="查看详情").click()
            await expect(page.locator("#history-detail")).to_be_focused()
            assert await page.locator("#history-detail").evaluate(
                "el => el.getBoundingClientRect().top < window.innerHeight"
            )
            await expect(page.locator("#history-ai-summary")).to_contain_text("已确认答案为 42。")
            await expect(page.locator("#history-summary-model")).to_contain_text("GPT-5.6 Luna")
            await expect(page.locator("#history-ai-summary img")).to_have_count(0)
            await expect(page.locator("#history-result")).to_contain_text('"answer": "42"')
            await expect(page.locator("#history-transcript")).to_contain_text(
                "Hello from the assistant."
            )
            await expect(page.locator("#history-transcript")).to_contain_text("The answer is 42.")
            await expect(page.locator("#history-transcript img")).to_have_count(0)
            async with page.expect_download() as pending:
                await page.get_by_role("link", name="下载 Transcript").click()
            downloaded = await pending.value
            assert downloaded.suggested_filename.endswith(".txt")
            text = Path(await downloaded.path()).read_text()
            assert "Hello from the assistant." in text and "The answer is 42." in text
            async with page.expect_download() as pending:
                await page.get_by_role("link", name="下载录音").click()
            downloaded = await pending.value
            with wave.open(str(await downloaded.path())) as audio:
                assert audio.getnchannels() == 2 and audio.getnframes() > 0
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await expect(page.get_by_role("tab")).to_have_count(3)
            private_write(directory / "pin.env", "AGENTCALL_TOKEN=9876\n")
            await page.get_by_role("tab", name="连接设备").click()
            await page.locator("#refresh-devices").click()
            await expect(page.locator("#login")).to_be_visible()
            await page.locator("#pin").fill("9876")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.locator("#device-list")).to_contain_text("Test Android")
            await page.locator("#logout").click()
            await expect(page.locator("#login")).to_be_visible()
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "browser": "Chromium",
                        "three_tabs": True,
                        "saved_pin_login": True,
                        "contacts_and_numbers": True,
                        "goal_is_completion": True,
                        "language_and_duration": True,
                        "voice_selection_and_provider_switch": True,
                        "transcription_enabled": True,
                        "result_and_paginated_transcript": True,
                        "transcript_xss_safe": True,
                        "detail_navigation_and_downloads": True,
                        "luna_summary_and_safe_text": True,
                        "mobile": True,
                        "live_pin_change_and_logout": True,
                        "real_calls": False,
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
