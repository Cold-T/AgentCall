import asyncio
import json

import httpx
import pytest
from conftest import DEVICE

from agentcall.api.app import create_app
from agentcall.storage.store import Store
from agentcall.tasks.manager import TaskManager
from agentcall.tasks.models import TaskInput
from agentcall.tasks.summary import SUMMARY_MODEL, TaskSummarizer


def ended_task(manager):
    task = manager.create(TaskInput(device=DEVICE, number="123", goal="Confirm opening hours"))
    manager.store.update_task(
        task["id"],
        state="ended",
        outcome="partial",
        model_result={"status": "partial", "result": {"time": "unconfirmed"}},
    )
    return task["id"]


def response(text="已确认明天营业；具体时间未确认。"):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {"type": "reasoning"},
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
            ],
        },
    )


async def test_summary_authenticated_deduplicated_cached_and_uses_exact_model(service, monkeypatch):
    backend = service[0]
    monkeypatch.setenv("AGENTCALL_TOKEN", "auth-test")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    manager = TaskManager(backend, backend.config)
    task_id = ended_task(manager)
    for _ in range(1000):
        backend.store.task_event(task_id, "model.transcript", {"text": ""})
    backend.store.task_event(
        task_id, "model.transcript", {"role": "user", "text": "We open tomorrow."}
    )
    requests = []
    gate = asyncio.Event()

    async def handler(request):
        requests.append(request)
        await gate.wait()
        return response()

    manager.summaries.transport = httpx.MockTransport(handler)
    app = create_app(backend.config, backend, manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(f"/tasks/{task_id}/summary")).status_code == 401
        client.headers["Authorization"] = "Bearer auth-test"
        first = asyncio.create_task(client.post(f"/tasks/{task_id}/summary"))
        second = asyncio.create_task(client.post(f"/tasks/{task_id}/summary"))
        while not requests:
            await asyncio.sleep(0)
        gate.set()
        a, b = await asyncio.gather(first, second)
        assert a.json() == b.json() and a.json()["status"] == "completed"
        assert len(requests) == 1
        sent = json.loads(requests[0].content)
        assert sent["model"] == SUMMARY_MODEL == "gpt-5.6-luna"
        assert sent["store"] is False and sent["reasoning"]["effort"] == "none"
        assert "We open tomorrow." in sent["input"]
        assert "unconfirmed" in sent["input"] and "partial" in sent["input"]
        assert "待总结的数据" in sent["instructions"]
        assert (await client.post(f"/tasks/{task_id}/summary")).json() == a.json()
        assert len(requests) == 1
        assert (await client.get(f"/tasks/{task_id}")).json()["summary"]["text"] == a.json()["text"]
        active = manager.create(TaskInput(device=DEVICE, number="123", goal="Not ended"))
        assert (await client.post(f"/tasks/{active['id']}/summary")).status_code == 400
        assert not any(command.startswith("ATD") for command in service[2].commands)
    await manager.close()


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_failure_is_safe_cached_and_can_retry(service, monkeypatch, status):
    backend = service[0]
    monkeypatch.setenv("OPENAI_API_KEY", "secret-error-key")
    manager = TaskManager(backend, backend.config)
    task_id = ended_task(manager)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(status, json={"error": "secret-error-key"}) if count == 1 else response()
        )

    manager.summaries.transport = httpx.MockTransport(handler)
    failed = await manager.summaries.summarize(task_id)
    assert failed["status"] == "failed" and str(status) in failed["error"]
    assert "secret-error-key" not in json.dumps(failed)
    assert await manager.summaries.summarize(task_id) == failed and count == 1
    assert (await manager.summaries.summarize(task_id, retry=True))["status"] == "completed"
    assert count == 2
    await manager.close()


async def test_missing_key_and_incomplete_response_do_not_fake_success(service, monkeypatch):
    backend = service[0]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manager = TaskManager(backend, backend.config)
    task_id = ended_task(manager)
    assert (await manager.summaries.summarize(task_id))["status"] == "failed"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    manager.summaries.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "incomplete", "output": []})
    )
    result = await manager.summaries.summarize(task_id, retry=True)
    assert result["status"] == "failed" and result["text"] is None
    assert backend.store.task(task_id)["model_result"]["status"] == "partial"
    await manager.close()


async def test_shutdown_marks_interrupted_summary_retryable(service, monkeypatch):
    backend = service[0]
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    manager = TaskManager(backend, backend.config)
    task_id = ended_task(manager)
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    manager.summaries.transport = httpx.MockTransport(handler)
    waiter = asyncio.create_task(manager.summaries.summarize(task_id))
    await entered.wait()
    await manager.close()
    await asyncio.gather(waiter, return_exceptions=True)
    assert backend.store.task_summary(task_id)["status"] == "failed"


async def test_summary_survives_database_reopen(tmp_path, service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    path = tmp_path / "data.sqlite3"
    store = Store(path)
    store.init_tasks()
    task_id = store.create_task(
        DEVICE, TaskInput(device=DEVICE, number="123", goal="Check").model_dump(), {}
    )
    store.update_task(task_id, state="ended")
    summarizer = TaskSummarizer(
        store, service[0].config, transport=httpx.MockTransport(lambda request: response())
    )
    await summarizer.summarize(task_id)
    await summarizer.close()
    store.close()
    reopened = Store(path)
    reopened.init_tasks()
    try:
        assert reopened.task(task_id)["summary"]["status"] == "completed"
    finally:
        reopened.close()
