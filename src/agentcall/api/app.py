import asyncio
import hmac
import json
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from agentcall.bluetooth.hfp import HFPError
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store
from agentcall.tasks.manager import TaskManager
from agentcall.tasks.models import TaskInput

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
Source = Literal["project", "pbap"]
TaskState = Literal["saved", "queued", "preparing", "dialing", "in_call", "finalizing", "ended"]


class Dial(BaseModel):
    device: str
    number: str | None = None
    contact_id: str | None = None


class Digits(BaseModel):
    digits: str = Field(pattern=r"^[0-9*#]{1,64}$")


class Context(BaseModel):
    text: str = Field(min_length=1, max_length=16000)


class Confirmation(BaseModel):
    accept: bool


class ConnectionBearer(HTTPBearer):
    async def __call__(self, request: HTTPConnection):
        return await super().__call__(request)


def authorized(header, token):
    return not token or hmac.compare_digest((header or "").encode(), f"Bearer {token}".encode())


def create_app(config=None, backend=None, task_manager=None):
    config = config or Config()
    owned_store = backend is None
    backend = backend or Backend(config, Store(config.database))
    task_manager = task_manager or TaskManager(backend, config)

    @asynccontextmanager
    async def lifespan(app):
        await backend.start()
        await task_manager.start()
        try:
            yield
        finally:
            await task_manager.close()
            await backend.close()
            if owned_store:
                backend.store.close()

    app = FastAPI(
        title="AgentCall service",
        version="0.1.0",
        root_path=config.root_path,
        lifespan=lifespan,
        description="Headless phone calls and AI tasks. Bearer authentication is required when configured. "
        "AT acceptance and model completion are separate from actual phone state.",
        dependencies=[Depends(ConnectionBearer(auto_error=False, scheme_name="BearerAuth"))],
    )
    app.state.backend = backend
    app.state.tasks = task_manager

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not authorized(request.headers.get("authorization"), config.token):
            return JSONResponse(
                {"detail": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.exception_handler(HFPError)
    async def hfp_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(TimeoutError)
    async def timeout(request, exc):
        return JSONResponse({"detail": str(exc) or "operation timed out"}, status_code=504)

    @app.get("/health", tags=["service"])
    async def health():
        return {
            "ready": backend.running,
            "bluetooth_error": backend.error,
            "adapter": config.adapter,
            "codec": config.codec,
        }

    @app.get("/devices", tags=["devices"])
    async def devices():
        return await backend.devices()

    @app.get("/devices/saved", tags=["devices"], summary="List last observed device snapshots")
    async def saved_devices():
        return backend.store.saved_devices()

    @app.get("/devices/{device}", tags=["devices"], summary="Get live or saved device information")
    async def device_detail(device: str):
        path = backend.path(device)
        if backend.running:
            for item in await backend.devices():
                if item["path"] == path:
                    return {"live": True, **item}
        for item in backend.store.saved_devices():
            if item["device"] == path:
                return {"live": False, **item}
        raise ValueError("unknown device")

    @app.post("/discovery/{action}", tags=["devices"])
    async def discovery(action: str):
        if action not in ("start", "stop"):
            raise ValueError("discovery action must be start or stop")
        if not backend.running:
            raise RuntimeError(backend.error or "BlueZ unavailable")
        await backend.bluez.call(
            f"/org/bluez/{config.adapter}",
            "org.bluez.Adapter1",
            "StartDiscovery" if action == "start" else "StopDiscovery",
        )
        return {"action": action, "accepted": True}

    @app.get("/pairing", tags=["devices"])
    async def pairing():
        return {"pending_ids": list(backend.bluez.agent.pending)}

    @app.post("/pairing/{request_id}", tags=["devices"])
    async def confirm(request_id: str, body: Confirmation):
        future = backend.bluez.agent.pending.get(request_id)
        if not future or future.done():
            raise ValueError("pairing confirmation expired or unknown")
        future.set_result(body.accept)
        return {"accepted": body.accept}

    @app.post("/devices/{device}/sync", tags=["contacts"])
    async def sync(device: str):
        return await backend.sync(device)

    @app.post("/devices/{device}/{action}", tags=["devices"])
    async def device_action(device: str, action: Literal["pair", "connect", "disconnect"]):
        return await backend.device_action(device, action)

    @app.get("/contacts", tags=["contacts"])
    async def contacts(
        q: str = "", device: str | None = None, limit: Limit = 100, offset: Offset = 0
    ):
        return backend.store.contacts(q, backend.path(device) if device else None, limit, offset)

    @app.get(
        "/contacts/{contact_id}", tags=["contacts"], summary="Get a contact and its source vCard"
    )
    async def contact(contact_id: str):
        return backend.store.contact(contact_id)

    @app.post("/calls", status_code=201, tags=["calls"])
    async def dial(body: Dial):
        if bool(body.number) == bool(body.contact_id):
            raise ValueError("provide exactly one of number or contact_id")
        return await backend.dial(body.device, body.number, body.contact_id)

    @app.get("/calls/current", tags=["calls"])
    async def current(device: str | None = None):
        path = backend.path(device) if device else None
        return [backend.store.call(c) for d, c in backend.current.items() if not path or d == path]

    @app.get("/calls", tags=["calls"])
    async def calls(
        device: str | None = None,
        source: Source | None = None,
        limit: Limit = 100,
        offset: Offset = 0,
    ):
        return backend.store.calls(backend.path(device) if device else None, source, limit, offset)

    @app.get("/calls/{call_id}", tags=["calls"])
    async def call(call_id: str, source: Source | None = None):
        result = backend.store.call_record(call_id, source)
        if not result:
            raise ValueError("unknown call ID")
        device = result["device"]
        result["audio"] = (
            backend.audio[device].status()
            if backend.current.get(device) == call_id and device in backend.audio
            else None
        )
        return result

    @app.post("/calls/{call_id}/answer", tags=["calls"])
    async def answer(call_id: str):
        await backend.slc(backend.call_device(call_id)).answer()
        return {"accepted": True}

    @app.post("/calls/{call_id}/hangup", tags=["calls"])
    async def hangup(call_id: str):
        await backend.slc(backend.call_device(call_id)).hangup()
        return {"accepted": True}

    @app.post("/calls/{call_id}/dtmf", tags=["calls"])
    async def dtmf(call_id: str, body: Digits):
        await backend.slc(backend.call_device(call_id)).dtmf(body.digits)
        return {"accepted": True}

    @app.get(
        "/events/history", tags=["events"], summary="Read persisted service events after a cursor"
    )
    async def event_history(
        after_id: Offset = 0,
        limit: Limit = 100,
        kind: str | None = None,
        device: str | None = None,
        call_id: str | None = None,
        task_id: str | None = None,
    ):
        return backend.store.event_history(
            after_id, limit, kind, backend.path(device) if device else None, call_id, task_id
        )

    @app.get("/events", tags=["events"])
    async def events(
        request: Request,
        kind: str | None = None,
        device: str | None = None,
        call_id: str | None = None,
        task_id: str | None = None,
    ):
        filters = {
            k: v
            for k, v in {
                "kind": kind,
                "device": backend.path(device) if device else None,
                "call_id": call_id,
                "task_id": task_id,
            }.items()
            if v is not None
        }

        async def stream():
            queue = asyncio.Queue(maxsize=256)
            backend.subscribers.add(queue)
            try:
                yield 'event: ready\ndata: {"kind":"events.ready"}\n\n'
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                        if event["kind"] != "events.overflow" and any(
                            event.get(k) != v for k, v in filters.items()
                        ):
                            continue
                        yield f"event: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                        if event["kind"] == "events.overflow":
                            return
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                backend.subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/tasks", status_code=201, tags=["tasks"])
    async def create_task(body: TaskInput):
        return task_manager.create(body)

    @app.get("/tasks", tags=["tasks"])
    async def tasks(
        state: TaskState | None = None,
        device: str | None = None,
        outcome: str | None = None,
        limit: Limit = 100,
        offset: Offset = 0,
    ):
        return [
            backend.store.task(i)
            for i in backend.store.task_ids(
                state, backend.path(device) if device else None, outcome, limit, offset
            )
        ]

    @app.get("/tasks/{task_id}", tags=["tasks"])
    async def task(task_id: str):
        result = backend.store.task(task_id)
        run = task_manager.runs.get(task_id)
        result["audio"] = run.bridge.status() if run and run.bridge else None
        return result

    @app.get("/tasks/{task_id}/events", tags=["tasks"])
    async def task_events(
        task_id: str, after_id: Offset = 0, limit: Limit = 100, kind: str | None = None
    ):
        return backend.store.task_events(task_id, after_id, limit, kind)

    @app.get(
        "/tasks/{task_id}/result",
        tags=["tasks"],
        summary="Get result, failure and linked phone state",
    )
    async def task_result(task_id: str):
        return backend.store.task_result(task_id)

    @app.get(
        "/tasks/{task_id}/tools", tags=["tasks"], summary="Get deduplicated tool execution records"
    )
    async def task_tools(task_id: str, limit: Limit = 100, offset: Offset = 0):
        return backend.store.task_tools(task_id, limit, offset)

    @app.post("/tasks/{task_id}/start", status_code=202, tags=["tasks"])
    async def start_task(task_id: str, idempotency_key: str | None = Header(default=None)):
        return task_manager.start_task(task_id, idempotency_key)

    @app.post("/tasks/{task_id}/cancel", status_code=202, tags=["tasks"])
    async def cancel_task(task_id: str):
        return await task_manager.cancel(task_id)

    @app.post("/tasks/{task_id}/context", tags=["tasks"])
    async def context(task_id: str, body: Context):
        await task_manager.update_context(task_id, body.text)
        return {"accepted": True}

    @app.websocket("/calls/{call_id}/audio")
    async def audio_socket(websocket: WebSocket, call_id: str):
        if not authorized(websocket.headers.get("authorization"), config.token):
            await websocket.close(code=1008)
            return
        try:
            device = backend.call_device(call_id)
        except (ValueError, HFPError):
            await websocket.close(code=1008)
            return
        audio = backend.audio.get(device)
        if audio is None or audio.closed or audio.owner:
            await websocket.close(code=1013)
            return
        audio.owner = True
        await websocket.accept()
        await websocket.send_json(audio.status())

        async def receive_phone():
            while True:
                await websocket.send_bytes(await audio.receive())

        async def send_phone():
            while True:
                pcm = await websocket.receive_bytes()
                if len(pcm) > 262144:
                    raise ValueError("audio message exceeds 256 KiB")
                await audio.send(pcm)

        tasks = [asyncio.create_task(receive_phone()), asyncio.create_task(send_phone())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, ConnectionError, OSError, ValueError) as exc:
            backend.emit("audio.client_closed", device=device, error=str(exc))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            audio.owner = False
            with suppress(RuntimeError):
                await websocket.close()

    return app
