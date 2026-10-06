import json

import pytest
from conftest import DEVICE

from agentcall.storage.store import Store

OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


async def test_filtered_paged_contacts_calls_and_pbap_details(service):
    backend, _, _, client, _ = service
    store = backend.store
    store.save_phonebook(
        DEVICE,
        [
            {"id": "c1", "name": "Alpha", "number": "123", "raw": "vcard1"},
            {"id": "c2", "name": "Beta", "number": "456", "raw": "vcard2"},
        ],
        [
            {
                "id": "p1",
                "number": "555",
                "started_at": "20261005T120000",
                "synced_at": "2026-10-05T13:00:00+00:00",
                "raw": "history",
                "book": "ich",
            }
        ],
    )
    store.save_phonebook(
        OTHER, [{"id": "c3", "name": "Gamma", "number": "789", "raw": "vcard3"}], []
    )
    contact = (await client.get("/contacts/c1")).json()
    assert contact["raw"] == "vcard1" and contact["device"] == DEVICE and contact["synced_at"]
    assert [
        r["id"]
        for r in (
            await client.get(
                "/contacts", params={"device": "11:22:33:44:55:66", "limit": 1, "offset": 1}
            )
        ).json()
    ] == ["c2"]
    assert (await client.get("/contacts", params={"q": "789"})).json()[0]["id"] == "c3"
    first = store.new_call(DEVICE, "123", "outgoing", "active")
    second = store.new_call(OTHER, "456", "outgoing", "idle")
    store.db.execute(
        "UPDATE calls SET answered_at=?,ended_at=? WHERE id=?",
        ("2026-10-05T12:00:00+00:00", "2026-10-05T12:00:45+00:00", first),
    )
    store.db.commit()
    assert (await client.get(f"/calls/{first}")).json()["duration_seconds"] == 45
    pbap = (
        await client.get("/calls", params={"source": "pbap", "device": "11:22:33:44:55:66"})
    ).json()
    assert [r["id"] for r in pbap] == ["p1"]
    assert pbap[0]["source"] == "pbap" and pbap[0]["duration_seconds"] is None
    assert (await client.get("/calls/p1", params={"source": "pbap"})).json()["raw"] == "history"
    assert (await client.post("/calls/p1/hangup")).status_code == 400
    projects = (await client.get("/calls", params={"source": "project", "limit": 1})).json()
    assert projects[0]["id"] == second
    assert (await client.get("/calls", params={"source": "project", "offset": 1})).json()[0][
        "id"
    ] == first
    backend.current[DEVICE] = first
    backend.current[OTHER] = second
    assert (
        len((await client.get("/calls/current", params={"device": "11:22:33:44:55:66"})).json())
        == 1
    )


async def test_task_result_tools_and_query_filters(service):
    backend, _, _, client, _ = service
    one = (
        await client.post(
            "/tasks", json={"device": "11:22:33:44:55:66", "number": "123", "goal": "Goal"}
        )
    ).json()["id"]
    two = (
        await client.post(
            "/tasks", json={"device": "AA:BB:CC:DD:EE:FF", "number": "456", "goal": "Other"}
        )
    ).json()["id"]
    store = backend.store
    store.update_task(
        one,
        state="ended",
        outcome="bluetooth_disconnected",
        model_result={"status": "completed", "result": {"answer": 42}},
        error={"message": "link lost"},
    )
    assert store.claim_tool(one, "t1", "send_dtmf", '{"digits":"1"}')
    store.finish_tool(one, "t1", {"ok": True})
    assert not store.claim_tool(one, "t1", "send_dtmf", '{"digits":"1"}')
    result = (await client.get(f"/tasks/{one}/result")).json()
    assert result["outcome"] == "bluetooth_disconnected"
    assert result["model_result"]["status"] == "completed" and result["error"]
    tools = (await client.get(f"/tasks/{one}/tools")).json()
    assert len(tools) == 1 and tools[0]["result"] == {"ok": True}
    assert [
        r["id"]
        for r in (
            await client.get("/tasks", params={"state": "saved", "device": "AA:BB:CC:DD:EE:FF"})
        ).json()
    ] == [two]
    assert [
        r["id"]
        for r in (await client.get("/tasks", params={"outcome": "bluetooth_disconnected"})).json()
    ] == [one]
    assert (await client.get("/tasks", params={"limit": 1, "offset": 1})).json()[0]["id"] == two
    assert (await client.get(f"/tasks/{one}/tools", params={"offset": 1})).json() == []


async def test_event_cursors_filters_and_pairing_ids_do_not_collide(service):
    backend, _, _, client, _ = service
    backend.emit("pairing.confirmation", device=DEVICE, id="pairing-request", passkey=123456)
    event = (await client.get("/events/history", params={"kind": "pairing.confirmation"})).json()[0]
    assert event["id"] == "pairing-request" and isinstance(event["event_id"], int)
    backend.emit("probe", device=OTHER, call_id="call2")
    wanted = backend.emit("probe", device=DEVICE, call_id="call1", task_id="task1")
    values = (
        await client.get(
            "/events/history",
            params={
                "after_id": event["event_id"],
                "kind": "probe",
                "device": "11:22:33:44:55:66",
                "call_id": "call1",
                "task_id": "task1",
            },
        )
    ).json()
    assert values == [wanted]
    task = (
        await client.post(
            "/tasks", json={"device": "11:22:33:44:55:66", "number": "123", "goal": "Goal"}
        )
    ).json()
    store = backend.store
    call_id = store.new_call(DEVICE, "123", "outgoing", "active")
    store.update_task(task["id"], call_id=call_id)
    # Use the manager owned by this API, with the same persistence and event contract.
    from agentcall.tasks.manager import TaskManager

    manager = TaskManager(backend, backend.config)
    manager.event(task["id"], "tool.result", call_id="model-tool-id", result={"ok": True})
    events = (await client.get("/events/history", params={"call_id": call_id})).json()
    assert events[-1]["tool_call_id"] == "model-tool-id"
    task_events = (
        await client.get(f"/tasks/{task['id']}/events", params={"kind": "tool.result", "limit": 1})
    ).json()
    assert task_events[0]["data"]["call_id"] == call_id
    assert (
        await client.get(f"/tasks/{task['id']}/events", params={"after_id": task_events[0]["id"]})
    ).json() == []
    await manager.close()


async def test_devices_saved_offline_and_openapi_auth(service, monkeypatch):
    backend, _, _, client, _ = service
    assert (await client.get("/devices")).status_code == 200
    live = (await client.get("/devices/11:22:33:44:55:66")).json()
    assert live["live"] and live["hfp_ready"]
    backend.running = False
    snapshot = (await client.get("/devices/11:22:33:44:55:66")).json()
    assert not snapshot["live"] and snapshot["last_observed"]["hfp_ready"]
    assert (await client.get("/devices/saved")).json()[0]["device"] == DEVICE
    await backend.connections[DEVICE].close()
    assert not (await client.get("/devices/11:22:33:44:55:66")).json()["last_observed"]["hfp_ready"]
    monkeypatch.setenv("AGENTCALL_TOKEN", "cp5-token")
    for path in (
        "/docs",
        "/openapi.json",
        "/devices/saved",
        "/tasks",
        "/events/history",
        "/events",
    ):
        denied = await client.get(path)
        assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
    monkeypatch.setenv("OPENAI_API_KEY", "server-openai-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "server-gemini-secret")
    for path in ("/health", "/devices/saved", "/tasks", "/contacts", "/calls", "/events/history"):
        response = await client.get(path, headers={"Authorization": "Bearer cp5-token"})
        assert response.status_code == 200
        assert (
            "server-openai-secret" not in response.text
            and "server-gemini-secret" not in response.text
        )
    schema = (
        await client.get("/openapi.json", headers={"Authorization": "Bearer cp5-token"})
    ).json()
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert schema["paths"]["/tasks"]["post"]["security"] == [{"BearerAuth": []}]
    assert "cp5-token" not in json.dumps(schema)


@pytest.mark.parametrize(
    "path",
    [
        "/calls?limit=0",
        "/contacts?offset=-1",
        "/tasks?state=bogus",
        "/calls?source=bogus",
        "/events/history?limit=1001",
        "/events/history?after_id=-1",
    ],
)
async def test_invalid_list_parameters_are_rejected(service, path):
    assert (await service[3].get(path)).status_code == 422


def test_device_and_event_migration_preserves_existing_data(tmp_path):
    path = str(tmp_path / "old.db")
    store = Store(path)
    store.reconnect(DEVICE, True)
    store.event("old.event", {"device": DEVICE})
    store.db.execute("DROP TABLE devices")  # emulate the schema before CP5
    store.db.commit()
    store.close()
    reopened = Store(path)
    assert reopened.saved_devices()[0]["device"] == DEVICE
    assert reopened.event_history()[0]["kind"] == "old.event"
    reopened.save_device(DEVICE, {"name": "Phone", "hfp_ag": True})
    reopened.close()
    again = Store(path)
    assert again.saved_devices()[0]["last_observed"]["name"] == "Phone"
    again.close()
