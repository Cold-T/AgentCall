import asyncio
import socket

import pytest
from conftest import DEVICE, Phone, until

from agentcall.bluetooth.hfp import HFPConnection, HFPError
from agentcall.vendor import at


async def test_handshake_and_state_only_from_phone(link):
    connection, phone, _ = link
    assert connection.ready
    assert phone.commands == ["AT+BRSF=4", "AT+CIND=?", "AT+CIND?", "AT+CMER=3,0,0,1", "AT+CLIP=1"]
    await connection.dial("+15551234567")
    assert phone.commands[-1] == "ATD+15551234567;"
    assert connection.state == "idle"  # OK does not mean connected
    await phone.send("+CIEV: 2,2\r\n+CIEV: 2,3\r\n+CIEV: 1,1\r\n+CIEV: 2,0\r\n")
    await until(lambda: connection.state == "active")
    await connection.dtmf("12*#")
    assert phone.commands[-4:] == ["AT+VTS=1", "AT+VTS=2", "AT+VTS=*", "AT+VTS=#"]
    await connection.hangup()
    assert connection.state == "active"
    await phone.send("+CIEV: 1,0\r\n")
    await until(lambda: connection.state == "idle")


async def test_fragmented_lines_and_initial_call(link):
    connection, phone, _ = link
    await phone.send("+CI")
    await asyncio.sleep(0.03)
    await phone.send("EV: 2,1\r")
    await until(lambda: connection.state == "incoming")
    await connection.answer()
    assert phone.commands[-1] == "ATA"
    await phone.send('+CLIP: "123",129\r\n+CIEV: 1,1\r\n+CIEV: 2,0\r\n')
    await until(lambda: connection.number == "123" and connection.state == "active")


async def test_idle_rejects_dtmf_and_invalid_commands_are_not_sent(link):
    connection, phone, _ = link
    count = len(phone.commands)
    for number in ("", "123\rAT+CHUP", "++123", "abc", "123;ATD999"):
        with pytest.raises(ValueError):
            await connection.dial(number)
    with pytest.raises(HFPError):
        await connection.dtmf("1")
    with pytest.raises(ValueError):
        await connection.dtmf("A")
    assert len(phone.commands) == count


@pytest.mark.parametrize("response", ["ERROR", "+CME ERROR: 30", "BUSY", "NO ANSWER"])
async def test_dial_error_propagates(link, response):
    connection, phone, events = link
    phone.task.cancel()
    await asyncio.gather(phone.task, return_exceptions=True)
    dial = asyncio.create_task(connection.dial("123"))
    await asyncio.sleep(0.01)
    await phone.send(response + "\r\n")
    with pytest.raises(HFPError):
        await dial
    assert connection.state == "idle"


async def test_command_timeout_closes_link_and_never_replays(link):
    connection, phone, _ = link
    phone.task.cancel()
    await asyncio.gather(phone.task, return_exceptions=True)
    with pytest.raises(HFPError, match="timeout"):
        await connection.dial("123")
    assert connection.closed
    with pytest.raises(HFPError):
        await connection.command("AT+CIND?")


async def test_initial_active_call_and_optional_clip_rejection():
    host, peer = socket.socketpair()
    phone = Phone(peer, initial="1,0,0", rejected={"AT+CLIP=1"})
    events = []
    connection = HFPConnection(host, DEVICE, lambda k, **d: events.append((k, d)), timeout=0.3)
    try:
        await connection.start()
        assert connection.state == "active" and connection.ready
    finally:
        await connection.close()
        await phone.close()


async def test_handshake_failure_does_not_become_ready():
    host, peer = socket.socketpair()
    phone = Phone(peer, rejected={"AT+CMER=3,0,0,1"})
    connection = HFPConnection(host, DEVICE, lambda *a, **k: None, timeout=0.3)
    try:
        with pytest.raises(HFPError):
            await connection.start()
        assert connection.closed and not connection.ready
    finally:
        await connection.close()
        await phone.close()


async def test_codec_negotiation_is_confirmed_serially():
    host, peer = socket.socketpair()
    phone = Phone(peer, features=1 << 9)
    events = []
    connection = HFPConnection(
        host, DEVICE, lambda k, **d: events.append((k, d)), "msbc", timeout=0.3
    )
    try:
        await connection.start()
        assert phone.commands[:2] == ["AT+BRSF=132", "AT+BAC=1,2"]
        await connection.command(at.cmd_bcc())
        await until(lambda: any(k == "audio.codec" for k, _ in events))
        assert "AT+BCS=2" in phone.commands and connection.codec == 2
    finally:
        await connection.close()
        await phone.close()


async def test_hold_state_and_disconnect(link):
    connection, phone, events = link
    await phone.send("+CIEV: 1,1\r\n+CIEV: 3,2\r\n")
    await until(lambda: connection.state == "held")
    await phone.close()
    await until(lambda: connection.closed)
    assert connection.state == "disconnected"
    assert sum(k == "hfp.disconnected" for k, _ in events) == 1


async def test_cancelled_inflight_command_closes_link(link):
    connection, phone, _ = link
    phone.task.cancel()
    await asyncio.gather(phone.task, return_exceptions=True)
    dial = asyncio.create_task(connection.dial("123"))
    await asyncio.sleep(0.01)
    dial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dial
    assert connection.closed


async def test_optional_command_timeout_cannot_emit_ready():
    host, peer = socket.socketpair()
    phone = Phone(peer)
    phone.ignored.add("AT+CLIP=1")
    events = []
    connection = HFPConnection(host, DEVICE, lambda k, **d: events.append((k, d)), timeout=0.05)
    try:
        with pytest.raises(HFPError):
            await connection.start()
        assert connection.closed and not connection.ready
        assert not any(k == "hfp.ready" for k, _ in events)
    finally:
        await connection.close()
        await phone.close()


async def test_cancellation_racing_completed_at_response_is_not_swallowed(link):
    connection, phone, _ = link
    phone.ignored.add("ATD123;")
    dial = asyncio.create_task(connection.dial("123"))
    await until(lambda: connection.pending is not None)
    # Deliver OK and cancellation before the command task resumes, as can occur during cleanup.
    connection.pending.set_result([])
    dial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dial
    assert connection.closed
    with pytest.raises(HFPError, match="not ready"):
        await connection.dial("456")
    assert "ATD456;" not in phone.commands
