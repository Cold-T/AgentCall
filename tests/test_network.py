import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
import websockets
from conftest import DEVICE

from agentcall.api.app import create_app
from agentcall.audio.transport import SCOAudio
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store


async def test_real_http_sse_and_full_duplex_websocket(monkeypatch):
    store = Store(":memory:")
    backend = Backend(Config(), store)

    async def start():
        backend.running = True

    monkeypatch.setattr(backend, "start", start)
    monkeypatch.setenv("AGENTCALL_TOKEN", "network-test")
    call_id = store.new_call(DEVICE, "123", "outgoing", "active")
    backend.current[DEVICE] = call_id
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    backend.audio[DEVICE] = SCOAudio(host, 1, mtu=48)
    app = create_app(backend.config, backend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    headers = {"Authorization": "Bearer network-test"}
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=headers
        ) as client:
            assert (await client.get("/health")).json()["ready"]
            assert (
                await client.get("/health", headers={"Authorization": "bad"})
            ).status_code == 401
            async with client.stream("GET", "/events") as events:
                lines = events.aiter_lines()
                assert await anext(lines) == "event: ready"
                assert '"events.ready"' in await anext(lines)
                backend.emit("verification.probe", device=DEVICE, value=42)
                while True:
                    line = await asyncio.wait_for(anext(lines), 1)
                    if line.startswith("data: ") and "verification.probe" in line:
                        assert json.loads(line[6:])["value"] == 42
                        break
            uri = f"ws://127.0.0.1:{port}/calls/{call_id}/audio"
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(uri):
                    pass
            async with websockets.connect(uri, additional_headers=headers) as ws:
                assert json.loads(await ws.recv())["sample_rate"] == 8000
                await ws.send(b"\x01\x02" * 20)
                assert (
                    await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 100), 1)
                    == b"\x01\x02" * 20
                )
                await asyncio.get_running_loop().sock_sendall(peer, b"\x03\x04" * 18)
                assert await asyncio.wait_for(ws.recv(), 1) == b"\x03\x04" * 18
                # Another attachment cannot steal the ongoing audio owner.
                with pytest.raises(websockets.exceptions.InvalidStatus):
                    async with websockets.connect(uri, additional_headers=headers):
                        pass
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        peer.close()
        listener.close()
        store.close()
