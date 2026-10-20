import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_live
import test_tasks
from conftest import DEVICE

from agentcall.providers.live import OpenAILive, live_tools
from agentcall.tasks.manager import TaskManager, TaskRun
from agentcall.tasks.models import TOOLS, TaskInput


@pytest.fixture
async def live_run(service):
    backend, _, _, _, _ = service
    manager = TaskManager(backend, backend.config)
    task = manager.create(
        TaskInput(device=DEVICE, number="123", goal="Get hours", config={"model": "gpt-live-1"})
    )
    run = TaskRun(manager, task)
    queue = asyncio.Queue()

    async def events():
        while True:
            yield await queue.get()

    run.provider = SimpleNamespace(
        revision=0,
        control_epoch=0,
        dtmf_epoch=0,
        dtmf_state="ready",
        menu_text="Test menu",
        delegation_id=None,
        closing=False,
        history=[],
        work=asyncio.Event(),
        dtmf_feedback_timeout=15.0,
        tools=live_tools(TOOLS),
        operations=[],
        playback_blocked=False,
        events=events,
        tool_result=AsyncMock(),
        append=AsyncMock(),
        backend=SimpleNamespace(render_farewell=AsyncMock(return_value=bytes(4800))),
    )
    for name in ("validate_operation", "reserve_dtmf", "complete_dtmf"):
        setattr(run.provider, name, getattr(OpenAILive, name).__get__(run.provider))
    run.bridge = SimpleNamespace(
        enqueue=AsyncMock(),
        interrupt=AsyncMock(),
        play_clip=AsyncMock(return_value=True),
        status=lambda: {},
    )
    run.hangup_phone = AsyncMock()
    manager.runs[run.id] = run
    try:
        yield run, queue
    finally:
        for child in run.children:
            child.cancel()
        await asyncio.gather(*run.children, return_exceptions=True)
        backend.listeners.discard(manager.on_event)
        await manager.summaries.close()


def tool(name, args, ident="call1", revision=0):
    if name == "finish_task":
        args = {"farewell": "谢谢，再见。", **args}
    return {
        "kind": "tool",
        "call_id": ident,
        "name": name,
        "arguments": json.dumps(args),
        "revision": revision,
        "control_epoch": 0,
        "dtmf_epoch": 0,
        "delegation_id": None,
    }


async def test_live_stale_tool_never_executes(live_run):
    run, _ = live_run
    run.provider.control_epoch = 1
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock()
    await run.live_tool(tool("send_dtmf", {"digits": "1"}))
    slc.dtmf.assert_not_awaited()
    assert "superseded" in run.provider.tool_result.call_args.args[1]["error"]


async def test_tool_execution_does_not_block_audio_or_cancel_on_backchannel(live_run):
    run, queue = live_run
    entered, release = asyncio.Event(), asyncio.Event()

    async def dtmf(digits):
        entered.set()
        await release.wait()

    run.backend.slc(run.device).dtmf = dtmf
    reader = asyncio.create_task(run.read_provider())
    try:
        await queue.put(tool("send_dtmf", {"digits": "1"}))
        await asyncio.wait_for(entered.wait(), 1)
        await queue.put({"kind": "transcript", "role": "user", "text": "嗯"})
        await queue.put({"kind": "audio", "pcm": bytes(40)})
        async with asyncio.timeout(1):
            while not run.bridge.enqueue.await_count:
                await asyncio.sleep(0)
        run.bridge.interrupt.assert_not_awaited()
        run.provider.tool_result.assert_not_awaited()
        release.set()
        async with asyncio.timeout(1):
            while not run.provider.tool_result.await_count:
                await asyncio.sleep(0)
        assert run.provider.tool_result.call_args.args[1]["ok"]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.parametrize("automatic", [False, True])
async def test_hangup_waits_for_complete_clip_without_response_done(live_run, automatic):
    run, _ = live_run
    entered, played = asyncio.Event(), asyncio.Event()

    async def play(pcm):
        entered.set()
        await played.wait()
        return True

    run.bridge.play_clip.side_effect = play
    if automatic:
        await run.live_tool(tool("finish_task", {"status": "completed", "result": {}}))
    else:
        run.store.update_task(run.id, model_result={"status": "completed", "result": {}})
        await run.live_tool(tool("hangup", {"reason": "done", "farewell": "谢谢，再见。"}))
    await asyncio.wait_for(entered.wait(), 1)
    run.hangup_phone.assert_not_awaited()
    assert run.provider.tool_result.call_args.args[1]["state"] == "scheduled"
    played.set()
    await asyncio.wait_for(run.hangup_job, 1)
    run.hangup_phone.assert_awaited_once()
    assert run.done.is_set()


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("during_clip", [False, True])
async def test_correction_revokes_pending_farewell_without_undoing_tools(
    live_run, during_clip, automatic
):
    run, _ = live_run
    entered = asyncio.Event()

    async def wait_forever(*args):
        entered.set()
        await asyncio.Event().wait()

    if during_clip:
        run.bridge.play_clip.side_effect = wait_forever
    else:
        run.provider.backend.render_farewell.side_effect = wait_forever
    run.store.update_task(run.id, model_result={"status": "completed", "result": {}})
    run.provider.operations = [{"call_id": "digits", "name": "send_dtmf", "result": {"ok": True}}]
    if automatic:
        await run.live_tool(tool("finish_task", {"status": "completed", "result": {}}, "finish"))
    else:
        await run.live_tool(tool("hangup", {"reason": "done", "farewell": "Goodbye."}))
    await asyncio.wait_for(entered.wait(), 1)
    run.provider.operations.append(
        {"call_id": "finish", "name": "finish_task", "result": {"ok": True}}
    )
    run.provider.revision += 1
    await run.revoke_live_hangup()
    assert run.provider.operations[-1]["result"]["state"] == "superseded"
    run.hangup_phone.assert_not_awaited()
    assert run.hangup_job is None
    assert not run.provider.playback_blocked
    assert run.store.task(run.id)["model_result"] is None
    assert run.provider.operations[0]["result"] == {"ok": True}
    assert run.bridge.interrupt.await_count == int(during_clip)


async def test_ending_call_rejects_new_work(live_run):
    run, _ = live_run
    run.hangup_requested = True
    await run.live_tool(tool("finish_task", {"status": "completed", "result": {}}))
    assert not run.provider.tool_result.call_args.args[1]["ok"]
    assert run.store.task(run.id)["model_result"] is None


async def test_live_model_selected_without_extra_switch(service, monkeypatch):
    backend, _, _, _, _ = service
    manager = TaskManager(backend, backend.config)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    try:
        task = manager.create(
            TaskInput(device=DEVICE, number="123", goal="hours", config={"model": "gpt-live-1"})
        )
        from agentcall.tasks.models import ProviderConfig

        provider = manager.make_provider(ProviderConfig(**task["config"]))
        assert provider.continuous_audio
        assert task["config"]["options"] == {}
        await provider.close()
    finally:
        backend.listeners.discard(manager.on_event)
        await manager.summaries.close()


# Reuse the socket-based phone/SCO rig; no Bluetooth device or external API is used.
payload, rig, wait_for = test_tasks.payload, test_tasks.rig, test_tasks.wait_for


class SimulatedLive:
    validate_operation = OpenAILive.validate_operation
    closing = False
    delegation_id = None
    continuous_audio = True
    input_rate = output_rate = 24000

    def __init__(self, rig):
        self.rig = rig
        self.revision = 0
        self.operations = []
        self.playback_blocked = False
        self.final_usage = None
        self.closed = asyncio.Event()
        self.queue = asyncio.Queue()
        self.started = False
        self.backend = SimpleNamespace(render_farewell=AsyncMock(return_value=b"\x01\x02" * 2400))

    async def open(self, instructions, tools):
        self.tools = live_tools(tools)

    async def start_response(self):
        call = self.rig.backend.store.call(self.rig.backend.current[DEVICE])
        assert call["state"] == "active"
        self.started = True
        await self.queue.put({"kind": "audio", "pcm": b"\x01\x02" * 2400})
        await self.queue.put(tool("finish_task", {"status": "completed", "result": {"answer": 42}}))

    async def events(self):
        while True:
            yield await self.queue.get()

    async def send_audio(self, pcm):
        assert self.started

    async def append(self, *args):
        pass

    async def tool_result(self, call_id, result):
        assert result["ok"]

    async def close(self):
        self.closed.set()


async def test_live_complete_phone_pipeline_and_no_speech_before_answer(rig):
    # This checks closure, not the shared fixture's 150 ms startup deadline.
    rig.backend.config.answer_timeout_seconds = 1
    rig.backend.config.audio_timeout_seconds = 1
    rig.backend.config.model_connect_seconds = 1
    provider = SimulatedLive(rig)
    rig.manager.factory = lambda config: provider
    task = rig.manager.create(
        TaskInput(
            **payload(
                config={"provider": "openai", "model": "gpt-live-1"},
                start_immediately=True,
            )
        )
    )
    await wait_for(lambda: rig.store.task(task["id"])["state"] == "ended")
    result = rig.store.task(task["id"])
    assert provider.started, (result["outcome"], result["error"])
    assert result["outcome"] == "completed", result["error"]
    assert provider.backend.render_farewell.await_count == 1
    assert rig.hangup_output_sizes and rig.hangup_output_sizes[0] > 0
    assert provider.closed.is_set()


async def test_unanswered_live_call_never_starts_voice_session(rig):
    rig.answer = False
    provider = SimulatedLive(rig)
    rig.manager.factory = lambda config: provider
    task = rig.manager.create(
        TaskInput(
            **payload(
                config={"provider": "openai", "model": "gpt-live-1"},
                start_immediately=True,
            )
        )
    )
    await wait_for(lambda: rig.store.task(task["id"])["state"] == "ended")
    assert not provider.started
    assert not rig.output


async def test_direct_hangup_stops_live_playback_before_phone_confirmation(live_run):
    run, _ = live_run
    call_id = run.store.new_call(run.device, "123", "outgoing", "active")
    run.store.update_task(run.id, call_id=call_id)
    await run.manager.stop_live_playback(call_id)
    run.bridge.interrupt.assert_awaited_once()
    assert run.provider.playback_blocked and run.hangup_requested
    await run.revoke_live_hangup()
    assert run.provider.playback_blocked  # A late transcript cannot undo explicit hangup.


async def test_call_ending_during_tool_preserves_unknown_outcome(live_run):
    run, _ = live_run
    started = asyncio.Event()

    async def dtmf(digits):
        started.set()
        await asyncio.Event().wait()

    run.backend.slc(run.device).dtmf = dtmf
    job = asyncio.create_task(run.live_tool(tool("send_dtmf", {"digits": "12"})))
    await asyncio.wait_for(started.wait(), 1)
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)
    row = run.store.db.execute(
        "SELECT result FROM tool_calls WHERE task_id=?", (run.id,)
    ).fetchone()
    assert json.loads(row["result"])["state"] == "unknown"


async def test_dtmf_failure_records_possible_partial_execution(live_run):
    from agentcall.bluetooth.hfp import HFPError

    run, _ = live_run
    run.backend.slc(run.device).dtmf = AsyncMock(side_effect=HFPError("after first digit"))
    await run.live_tool(tool("send_dtmf", {"digits": "12"}))
    result = run.provider.tool_result.call_args.args[1]
    assert result["state"] == "unknown"
    assert "Do not retry" in result["error"]


async def test_old_queued_audio_is_discarded_after_playback_unblocks(live_run):
    run, queue = live_run
    run.provider.audio_generation = 2
    reader = asyncio.create_task(run.read_provider())
    try:
        await queue.put({"kind": "audio", "pcm": bytes(40), "generation": 0})
        await queue.put({"kind": "audio", "pcm": bytes(20), "generation": 2})
        async with asyncio.timeout(1):
            while not run.bridge.enqueue.await_count:
                await asyncio.sleep(0)
        assert run.bridge.enqueue.await_count == 1
        assert run.bridge.enqueue.call_args.args[0] == bytes(20)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.parametrize("failure", ["phone", "input"])
async def test_live_startup_is_cancelled_promptly_when_call_or_input_ends(rig, failure):
    entered, stopped = asyncio.Event(), asyncio.Event()

    class SlowLive(SimulatedLive):
        async def start_response(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def send_audio(self, pcm):
            if failure == "input":
                raise RuntimeError("startup input backlog exceeded")

    provider = SlowLive(rig)
    rig.manager.factory = lambda config: provider
    task = rig.manager.create(
        TaskInput(
            **payload(
                config={"provider": "openai", "model": "gpt-live-1"},
                max_call_seconds=30,
                start_immediately=True,
            )
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "phone":
        await rig.phone.send("+CIEV: 1,0\r\n+CIEV: 2,0\r\n")
    await asyncio.wait_for(stopped.wait(), 0.5)
    await wait_for(lambda: rig.store.task(task["id"])["state"] == "ended")
    assert provider.closed.is_set()
    assert not provider.started
    if failure == "input":
        assert rig.store.task(task["id"])["error"]["message"] == "startup input backlog exceeded"


async def test_local_stop_failure_does_not_block_explicit_phone_hangup(live_run):
    run, _ = live_run
    call_id = run.store.new_call(run.device, "123", "outgoing", "active")
    run.store.update_task(run.id, call_id=call_id)
    run.bridge.interrupt.side_effect = OSError("audio socket closed")
    # The API awaits this hook before sending AT+CHUP; it must still return.
    await run.manager.stop_live_playback(call_id)
    assert run.provider.playback_blocked and run.hangup_requested
    assert any(e["kind"] == "task.cleanup_error" for e in run.store.task_events(run.id))


@pytest.mark.parametrize("deliver_event", [False, True])
async def test_new_delegation_revokes_pending_hangup_without_new_transcript(
    live_run, deliver_event
):
    run, queue = live_run
    entered, release = asyncio.Event(), asyncio.Event()
    run.provider.delegation_id = "old"

    async def render(*args):
        entered.set()
        await release.wait()
        return bytes(4800)

    run.provider.backend.render_farewell.side_effect = render
    run.store.update_task(run.id, model_result={"status": "completed", "result": {}})
    event = tool("hangup", {"reason": "done", "farewell": "Goodbye."})
    event["delegation_id"] = "old"
    await run.live_tool(event)
    await asyncio.wait_for(entered.wait(), 1)
    job = run.hangup_job
    run.provider.delegation_id = "new"
    reader = None
    try:
        if deliver_event:
            reader = asyncio.create_task(run.read_provider())
            await queue.put({"kind": "delegation", "delegation_id": "new", "revision": 0})
            async with asyncio.timeout(1):
                while run.hangup_job is not None:
                    await asyncio.sleep(0)
        else:
            # The provider may see the new delegation before the manager consumes it.
            release.set()
            await asyncio.wait_for(job, 1)
        run.hangup_phone.assert_not_awaited()
        assert run.hangup_job is None
    finally:
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def test_live_drains_ringing_audio_and_preserves_first_answer_packet(live_run):
    import socket

    from agentcall.audio.transport import SCOAudio

    run, _ = live_run
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 1, mtu=480)
    run.backend.audio[run.device] = audio
    call = {"state": "alerting", "ended_at": None}
    run.linked_call = lambda: call
    waiting = asyncio.create_task(run.wait_connected())
    try:
        # More than Live's two-second startup limit arrives before answer.
        for _ in range(100):
            await asyncio.get_running_loop().sock_sendall(peer, b"\x01\x00" * 240)
        await wait_for(lambda: audio.rx_bytes == 48000)
        assert not waiting.done()
        call["state"] = "active"
        first_words = b"\x02\x00" * 240
        await asyncio.get_running_loop().sock_sendall(peer, first_words)
        assert await asyncio.wait_for(waiting, 1) is audio
        assert await audio.receive() == first_words
        assert audio.rx_bytes == 48480
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        audio.close()
        peer.close()


async def test_blank_farewell_does_not_commit_result_or_schedule_hangup(live_run):
    run, _ = live_run
    await run.live_tool(
        tool("finish_task", {"status": "completed", "result": {}, "farewell": "   "})
    )
    assert not run.provider.tool_result.call_args.args[1]["ok"]
    assert run.store.task(run.id)["model_result"] is None
    assert run.hangup_job is None


async def test_duplicate_closure_does_not_play_farewell_twice(live_run):
    run, _ = live_run
    entered, release = asyncio.Event(), asyncio.Event()

    async def render(*args):
        entered.set()
        await release.wait()
        return bytes(4800)

    run.provider.backend.render_farewell.side_effect = render
    await run.live_tool(tool("finish_task", {"status": "completed", "result": {}}))
    await asyncio.wait_for(entered.wait(), 1)
    job = run.hangup_job
    await run.live_tool(tool("hangup", {"reason": "done", "farewell": "Goodbye."}, "extra"))
    assert run.hangup_job is job
    release.set()
    await asyncio.wait_for(job, 1)
    run.provider.backend.render_farewell.assert_awaited_once()
    run.bridge.play_clip.assert_awaited_once()
    run.hangup_phone.assert_awaited_once()


async def test_dtmf_survives_transcript_but_not_duplicate_navigation(live_run):
    run, _ = live_run
    run.provider.revision = 40
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock()
    await run.live_tool(tool("send_dtmf", {"digits": "2"}))
    assert run.provider.dtmf_state == "awaiting_feedback"
    assert run.provider.dtmf_epoch == 1
    await run.live_tool(tool("send_dtmf", {"digits": "2"}, ident="duplicate"))
    slc.dtmf.assert_awaited_once_with("2")
    assert not run.provider.tool_result.call_args.args[1]["ok"]
    # A confirmed new menu may legitimately use the same key again.
    run.provider.dtmf_state = "ready"
    run.provider.menu_text = "New menu"
    event = tool("send_dtmf", {"digits": "2"}, ident="new-menu")
    event["dtmf_epoch"] = 1
    await run.live_tool(event)
    assert slc.dtmf.await_count == 2


async def test_queued_dtmf_rechecks_control_after_lock(live_run):
    run, _ = live_run
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock()
    async with run.live_tool_lock:
        job = asyncio.create_task(run.live_tool(tool("send_dtmf", {"digits": "2"})))
        await asyncio.sleep(0)
        run.provider.control_epoch += 1
    await job
    slc.dtmf.assert_not_awaited()


async def test_dtmf_unknown_blocks_new_call_id(live_run):
    from agentcall.bluetooth.hfp import HFPError

    run, _ = live_run
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock(side_effect=HFPError("partial"))
    await run.live_tool(tool("send_dtmf", {"digits": "12"}))
    assert run.provider.dtmf_state == "uncertain"
    await run.live_tool(tool("send_dtmf", {"digits": "12"}, ident="retry"))
    assert slc.dtmf.await_count == 1


async def test_navigation_no_progress_ends_incomplete(live_run):
    run, queue = live_run
    await queue.put({"kind": "dtmf_no_progress"})
    await run.read_provider()
    assert run.done.is_set()
    assert run.failure.outcome == "incomplete"


# Real Live WebSocket adapter -> task manager -> socket HFP phone, without external APIs.
live_server = test_live.live_server


async def test_live_dtmf_phone_pipeline_during_continuous_transcription(rig, live_server):
    for attr in ("answer_timeout_seconds", "audio_timeout_seconds", "model_connect_seconds"):
        setattr(rig.backend.config, attr, 1)
    entered, release = asyncio.Event(), asyncio.Event()
    reasoning = AsyncMock()
    reasoning.review_dtmf.return_value = "information"
    reasoning.render_farewell.return_value = b"\x01\x02" * 2400

    async def decide(history, operations, execute, **kwargs):
        if not operations:
            entered.set()
            await release.wait()
            await execute({"call_id": "key", "name": "send_dtmf", "arguments": '{"digits":"2"}'})
        else:
            assert operations[0]["result"]["state"] == "accepted"
            await execute(
                {
                    "call_id": "finish",
                    "name": "finish_task",
                    "arguments": json.dumps(
                        {
                            "status": "completed",
                            "result": {"answer": 42},
                            "farewell": "Goodbye",
                        }
                    ),
                }
            )
        return ""

    reasoning.run.side_effect = decide
    provider = test_live.make_provider(live_server, reasoning)
    provider.dtmf_quiet = 0.01
    provider.dtmf_window = 0.02
    rig.manager.factory = lambda config: provider
    task = rig.manager.create(
        TaskInput(
            **payload(
                config={"provider": "openai", "model": "gpt-live-1"},
                max_call_seconds=5,
                start_immediately=True,
            )
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    for fragment in ["Hours press two.", " Returns press three.", " Pharmacy press four."]:
        await test_live.send(
            live_server, {"type": "session.input_transcript.delta", "delta": fragment}
        )
    await wait_for(lambda: provider.revision == 3)
    release.set()
    await wait_for(lambda: provider.dtmf_state == "awaiting_feedback")
    await test_live.send(
        live_server,
        {"type": "session.input_transcript.delta", "delta": " Today we close at eight thirty."},
    )
    await wait_for(lambda: rig.store.task(task["id"])["state"] == "ended")
    result = rig.store.task(task["id"])
    assert result["outcome"] == "completed", result["error"]
    assert rig.phone.commands.count("AT+VTS=2") == 1
    assert rig.phone.commands.count("AT+CHUP") == 1


async def test_costco_replay_opens_then_navigates_once_and_listens(rig, live_server):
    """Regression for the real failure: opening completed before any IVR input."""
    for attr in ("answer_timeout_seconds", "audio_timeout_seconds", "model_connect_seconds"):
        setattr(rig.backend.config, attr, 1)
    opened, repeat_blocked = asyncio.Event(), asyncio.Event()
    reasoning = AsyncMock()
    reasoning.render_farewell.return_value = b"\x01\x02" * 2400

    async def review(before, after, operations):
        return "menu" if "For current hours" in after else "information"

    async def decide(history, operations, execute, *, allow_dtmf=True, **kwargs):
        text = " ".join(row["content"] for row in history)
        if not history:
            opened.set()
            return "confirm today's closing time"
        if not operations:
            assert allow_dtmf
            await execute(
                {"call_id": "hours-menu", "name": "send_dtmf", "arguments": '{"digits":"2"}'}
            )
        elif len(operations) == 1:
            assert allow_dtmf
            await execute({"call_id": "hours", "name": "send_dtmf", "arguments": '{"digits":"1"}'})
        elif "eight thirty p.m." not in text:
            assert not allow_dtmf
            # Even a misbehaving backend cannot send a repeated key while information plays.
            result = await execute(
                {"call_id": "bad-repeat", "name": "send_dtmf", "arguments": '{"digits":"1"}'}
            )
            assert result["state"] == "rejected"
            repeat_blocked.set()
        else:
            assert not allow_dtmf
            await execute(
                {
                    "call_id": "finish",
                    "name": "finish_task",
                    "arguments": json.dumps(
                        {
                            "status": "completed",
                            "result": {"closing_time": "20:30"},
                            "farewell": "Thank you, goodbye.",
                        }
                    ),
                }
            )
        return ""

    reasoning.review_dtmf.side_effect = review
    reasoning.run.side_effect = decide
    provider = test_live.make_provider(live_server, reasoning)
    provider.backend_quiet = provider.dtmf_quiet = 0.01
    provider.backend_window = provider.dtmf_window = 0.03
    rig.manager.factory = lambda config: provider
    task = rig.manager.create(
        TaskInput(
            **payload(
                config={"provider": "openai", "model": "gpt-live-1", "language": "English"},
                goal="Confirm today closing time",
                result_schema={"type": "object"},
                max_call_seconds=8,
                start_immediately=True,
            )
        )
    )
    await asyncio.wait_for(opened.wait(), 2)
    await asyncio.sleep(0.05)

    async def say(text):
        await test_live.send(live_server, {"type": "session.input_transcript.delta", "delta": text})

    await say("For warehouse hours, directions and holidays observed, press two.")
    await wait_for(lambda: provider.dtmf_epoch == 1 and provider.dtmf_state == "awaiting_feedback")
    await say("For current hours press one. For directions press two.")
    await wait_for(lambda: provider.dtmf_epoch == 2 and provider.dtmf_state == "awaiting_feedback")
    await say(
        "Our hours are as follows. Executive members only, Monday through Friday nine to ten."
    )
    await asyncio.wait_for(repeat_blocked.wait(), 2)
    assert rig.phone.commands.count("AT+VTS=1") == 1
    await say(" All members Monday through Friday until eight thirty p.m.")
    await wait_for(lambda: rig.store.task(task["id"])["state"] == "ended")
    result = rig.store.task(task["id"])
    assert result["outcome"] == "completed", result["error"]
    assert result["model_result"]["result"]["closing_time"] == "20:30"
    assert rig.phone.commands.count("AT+VTS=2") == 1
    assert rig.phone.commands.count("AT+VTS=1") == 1
    assert rig.phone.commands.count("AT+CHUP") == 1
    assert provider.control_epoch == 0 and provider.delegation_id is None


async def test_task_executor_rejects_dtmf_during_information_playback(live_run):
    run, _ = live_run
    run.provider.dtmf_state = "listening"
    run.provider.dtmf_epoch = 2
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock()
    event = tool("send_dtmf", {"digits": "1"}, ident="late-repeat")
    event["dtmf_epoch"] = 2
    await run.live_tool(event)
    slc.dtmf.assert_not_awaited()
    assert not run.provider.tool_result.call_args.args[1]["ok"]


async def test_model_selected_zero_is_sent_without_menu_whitelist(live_run):
    run, _ = live_run
    slc = run.backend.slc(run.device)
    slc.dtmf = AsyncMock()
    await run.live_tool(tool("send_dtmf", {"digits": "0"}))
    slc.dtmf.assert_awaited_once_with("0")
    assert run.provider.tool_result.call_args.args[1]["ok"]


async def test_repeated_recording_does_not_revoke_result_but_new_speech_does(live_run):
    run, queue = live_run
    run.store.update_task(
        run.id, model_result={"status": "completed", "result": {"hours": "8:30 PM"}}
    )
    reader = asyncio.create_task(run.read_provider())
    try:
        await queue.put(
            {
                "kind": "transcript",
                "role": "user",
                "text": "For hours press one.",
                "repeated_recording": True,
            }
        )
        await asyncio.sleep(0.02)
        assert run.store.task(run.id)["model_result"] is not None
        await queue.put(
            {"kind": "transcript", "role": "user", "text": "Actually we close early today."}
        )
        async with asyncio.timeout(1):
            while run.store.task(run.id)["model_result"] is not None:
                await asyncio.sleep(0)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_repeated_recording_allows_farewell_and_hangup(live_run):
    run, queue = live_run
    entered, played = asyncio.Event(), asyncio.Event()

    async def play(pcm):
        entered.set()
        await played.wait()
        return True

    run.bridge.play_clip.side_effect = play
    reader = asyncio.create_task(run.read_provider())
    try:
        await run.live_tool(
            tool("finish_task", {"status": "partial", "result": {"exception": "unknown"}})
        )
        await asyncio.wait_for(entered.wait(), 1)
        job = run.hangup_job
        await queue.put(
            {
                "kind": "transcript",
                "role": "user",
                "text": "For hours press one.",
                "repeated_recording": True,
            }
        )
        await asyncio.sleep(0.02)
        assert run.hangup_job is job
        played.set()
        await asyncio.wait_for(job, 1)
        run.hangup_phone.assert_awaited_once()
        assert run.store.task(run.id)["model_result"]["status"] == "partial"
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
