import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from agentcall.providers.base import ProviderError
from agentcall.providers.live import OpenAILive
from agentcall.tasks.models import TOOLS, ProviderConfig


@pytest.fixture
async def live_server():
    messages, sockets = [], []

    async def handler(ws):
        sockets.append(ws)
        try:
            async for raw in ws:
                event = json.loads(raw)
                messages.append(event)
                if event["type"] == "session.start":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.started",
                                "session": {
                                    "audio": {"format": {"type": "audio/pcm", "rate": 24000}},
                                },
                            }
                        )
                    )
                elif event["type"] == "session.close":
                    await ws.send(json.dumps({"type": "session.closed", "usage": {"tokens": 9}}))
        except websockets.exceptions.ConnectionClosed:
            pass

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/live/sessions", messages, sockets


def make_provider(server, backend=None):
    provider = OpenAILive(
        ProviderConfig(model="gpt-live-1"),
        "test-secret",
        endpoint=server[0],
        timeout=0.5,
        backend=backend or AsyncMock(),
    )
    provider.backend.review_menu.return_value = "menu"
    return provider


async def next_kind(provider, kind):
    async with asyncio.timeout(1):
        while True:
            event = await provider.queue.get()
            if isinstance(event, Exception):
                raise event
            if event["kind"] == kind:
                return event


async def send(server, event):
    await server[2][0].send(json.dumps(event))


async def delegate(server, ident="d1"):
    await send(
        server,
        {
            "type": "session.delegation.created",
            "offset_ms": 120,
            "delegation": {"id": ident, "target": "client"},
        },
    )


async def test_live_waits_for_answer_hook_and_uses_continuous_protocol(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("private backend task", TOOLS)
        await provider.send_audio(bytes(20))
        assert not live_server[1] and not live_server[2]
        provider.conversation_instructions = "Only dialogue instructions"
        await provider.start_response()
        await provider.start_response()
        session = live_server[1][0]["session"]
        assert session["model"] == "gpt-live-1"
        assert session["instructions"] == "Only dialogue instructions"
        assert session["delegation"] == {"type": "client"}
        assert session["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert "answered" in session["input"][0]["content"][0]["text"]
        assert "private backend task" not in json.dumps(session)
        await provider.send_audio(bytes(20))
        # Both the startup-buffered and post-start packets must reach the server.
        # Closing cancels the sender, so do not race it against this assertion.
        async with asyncio.timeout(1):
            while sum(e["type"] == "session.input_audio.append" for e in live_server[1]) < 2:
                await asyncio.sleep(0)
        await send(
            live_server,
            {"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(40)).decode()},
        )
        assert (await next_kind(provider, "audio"))["pcm"] == bytes(40)
        with pytest.raises(ProviderError, match="no item truncation"):
            await provider.truncate_audio("item", 0, 10)
        with pytest.raises(ValueError, match="complete"):
            await provider.send_audio(b"x")
    finally:
        await provider.close()
    assert provider.final_usage == {"tokens": 9}
    assert [event["type"] for event in live_server[1]] == [
        "session.start",
        "session.input_audio.append",
        "session.input_audio.append",
        "session.close",
    ]
    assert all(base64.b64decode(event["audio"]) == bytes(20) for event in live_server[1][1:3])
    assert (
        "farewell"
        not in next(t for t in TOOLS if t["name"] == "hangup")["parameters"]["properties"]
    )


async def test_transcript_preserves_fragments_times_and_revisions(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        for role, fragment, start, end, revision in [
            ("input", " yes", 10, 30, 1),
            ("input", " yes", 30, 40, 2),
            ("output", " okay", 42, 80, 2),
        ]:
            await send(
                live_server,
                {
                    "type": f"session.{role}_transcript.delta",
                    "delta": fragment,
                    "start_ms": start,
                    "end_ms": end,
                },
            )
            event = await next_kind(provider, "transcript")
            assert event["text"] == fragment
            assert (event["start_ms"], event["end_ms"]) == (start, end)
            assert event["revision"] == revision
            assert event["delta"] is True
        assert [item["content"] for item in provider.history] == [" yes yes", " okay"]
    finally:
        await provider.close()


async def test_audio_continues_during_backend_and_duplicate_delegation_is_ignored(live_server):
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(history, operations, execute, **kwargs):
        entered.set()
        await release.wait()
        return "The result"

    backend = AsyncMock()
    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await delegate(live_server)
        event = await next_kind(provider, "delegation")
        assert event["delegation_id"] == "d1" and event["revision"] == 0
        await asyncio.wait_for(entered.wait(), 1)
        await delegate(live_server)
        await provider.send_audio(bytes(10))
        await send(
            live_server,
            {"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(12)).decode()},
        )
        assert (await next_kind(provider, "audio"))["pcm"] == bytes(12)
        release.set()
        async with asyncio.timeout(1):
            while not any(e["type"] == "session.commentary.append" for e in live_server[1]):
                await asyncio.sleep(0.001)
        commentary = next(e for e in live_server[1] if e["type"] == "session.commentary.append")
        assert commentary["delegation_id"] == "d1" and commentary["content"] == "The result"
        assert backend.run.await_count == 1
    finally:
        await provider.close()


async def test_revision_blocks_future_tools_suppresses_stale_result_and_reasons_again(live_server):
    entered, release, refreshed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
            attempts.append("attempted")
            await execute({"call_id": "obsolete", "name": "send_dtmf", "arguments": "{}"})
            attempts.append("continued")
            return "STALE RESULT MUST NOT BE SPOKEN"
        assert history[-1]["content"] == "Thursday instead"
        refreshed.set()
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await delegate(live_server)
        await asyncio.wait_for(entered.wait(), 1)
        await provider.update_context("Thursday instead")
        release.set()
        stale = await next_kind(provider, "backend_stale")
        assert stale["revision"] == 0 and stale["current_revision"] == 1
        await asyncio.wait_for(refreshed.wait(), 1)
        assert attempts == ["attempted"]
        assert not provider.pending_tools and not provider.operations
        assert not any(e["type"] == "session.commentary.append" for e in live_server[1])
    finally:
        await provider.close()


async def test_in_flight_tool_finishes_and_ledger_reaches_next_revision(live_server):
    entered, refreshed = asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not entered.is_set():
            entered.set()
            result = await execute(
                {"call_id": "sent", "name": "send_dtmf", "arguments": '{"digits":"1"}'}
            )
            assert result == {"ok": True}
            return "old result"
        assert operations == provider.operations
        assert operations[0]["result"] == {"ok": True}
        assert operations[0]["revision"] == 0
        refreshed.set()
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.menu_text = provider.menu_buffer = "Complete test menu"
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await delegate(live_server)
        event = await next_kind(provider, "tool")
        assert event["revision"] == 0 and event["delegation_id"] == "d1"
        await send(live_server, {"type": "session.input_transcript.delta", "delta": "change that"})
        await next_kind(provider, "transcript")
        await provider.tool_result("sent", {"ok": True})
        await delegate(live_server, "d2")
        await asyncio.wait_for(refreshed.wait(), 1)
        assert len(provider.operations) == 1
        assert not provider.pending_tools
        with pytest.raises(ProviderError, match="Unknown"):
            await provider.tool_result("missing", {})
    finally:
        await provider.close()


async def test_backend_errors_do_not_leak_and_are_not_retried(live_server):
    backend = AsyncMock()
    backend.run.side_effect = RuntimeError("secret-token private-data")
    provider = make_provider(live_server, backend)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await delegate(live_server)
        with pytest.raises(ProviderError) as error:
            await next_kind(provider, "never")
        assert "secret-token" not in str(error.value)
        assert "private-data" not in str(error.value)
        assert backend.run.await_count == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("delta", ["not base64", base64.b64encode(b"x").decode()])
async def test_invalid_audio_fails_without_forwarding_pcm(live_server, delta):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await send(live_server, {"type": "session.output_audio.delta", "delta": delta})
        with pytest.raises(ProviderError, match="invalid PCM"):
            await next_kind(provider, "audio")
    finally:
        # A failed reader cannot acknowledge session.close.
        provider.closed.set()
        await provider.close()


async def test_playback_block_drops_live_audio_but_keeps_user_corrections(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        provider.playback_blocked = True
        await send(
            live_server,
            {"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(12)).decode()},
        )
        await send(live_server, {"type": "session.input_transcript.delta", "delta": "Wait!"})
        event = await asyncio.wait_for(provider.queue.get(), 1)
        assert event["kind"] == "transcript" and event["text"] == "Wait!"
        assert provider.revision == 1
    finally:
        await provider.close()


async def test_unsupported_negotiated_audio_format_is_rejected():
    async def handler(ws):
        await ws.recv()
        await ws.send(
            json.dumps(
                {
                    "type": "session.started",
                    "session": {
                        "audio": {"format": {"type": "audio/pcm", "rate": 16000}},
                    },
                }
            )
        )
        await ws.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        provider = make_provider((f"ws://127.0.0.1:{port}", [], []))
        try:
            await provider.open("task", TOOLS)
            with pytest.raises(ProviderError, match="unsupported audio format"):
                await provider.start_response()
            assert not provider.started
        finally:
            await provider.close()


async def test_startup_audio_is_preserved_and_paced(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        first, second = b"\x01\x00" * 480, b"\x02\x00" * 480
        await provider.send_audio(first)
        await provider.send_audio(second)
        assert not live_server[1]
        await provider.start_response()
        async with asyncio.timeout(1):
            while len([e for e in live_server[1] if e["type"] == "session.input_audio.append"]) < 2:
                await asyncio.sleep(0.001)
        events = [e for e in live_server[1] if e["type"] == "session.input_audio.append"]
        assert [base64.b64decode(e["audio"]) for e in events] == [first, second]
    finally:
        await provider.close()


async def test_startup_audio_overflow_is_explicit(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        await provider.send_audio(bytes(96000))
        with pytest.raises(ProviderError, match="backlog"):
            await provider.send_audio(bytes(2))
    finally:
        await provider.close()


async def test_append_preserves_whole_instruction_and_rejects_oversize(live_server):
    provider = make_provider(live_server)
    provider.send = AsyncMock()
    content = "Do not repeat the operation. " * 5
    await provider.append("instructions", content)
    assert provider.send.await_count == 1
    assert provider.send.call_args.args[0]["content"] == content
    with pytest.raises(ProviderError, match="coherent"):
        await provider.append("commentary", "界" * 167)
    assert provider.send.await_count == 1


async def test_new_delegation_suppresses_old_result_without_new_transcript(live_server):
    entered, release = asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
            return "Old answer"
        return "Current answer"

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await delegate(live_server, "old")
        await asyncio.wait_for(entered.wait(), 1)
        await delegate(live_server, "new")
        async with asyncio.timeout(1):
            while provider.delegation_id != "new":
                await asyncio.sleep(0)
        release.set()
        await next_kind(provider, "backend_stale")
        async with asyncio.timeout(1):
            while not any(e["type"] == "session.commentary.append" for e in live_server[1]):
                await asyncio.sleep(0.001)
        replies = [e for e in live_server[1] if e["type"] == "session.commentary.append"]
        assert [(e["delegation_id"], e["content"]) for e in replies] == [("new", "Current answer")]
    finally:
        await provider.close()


async def test_backend_is_closed_even_when_socket_close_fails(live_server):
    provider = make_provider(live_server)
    provider.ws = AsyncMock()
    provider.ws.close.side_effect = OSError("broken transport")
    with pytest.raises(OSError):
        await provider.close()
    provider.backend.close.assert_awaited_once()


async def test_connected_session_requests_first_task_step_without_user_speech(live_server):
    backend = AsyncMock()
    backend.run.return_value = "Ask when the office opens."
    provider = make_provider(live_server, backend)
    try:
        await provider.open("task", TOOLS)
        backend.run.assert_not_awaited()
        await provider.start_response()
        async with asyncio.timeout(1):
            while not any(e["type"] == "session.commentary.append" for e in live_server[1]):
                await asyncio.sleep(0.01)
        backend.run.assert_awaited_once()
        update = next(e for e in live_server[1] if e["type"] == "session.commentary.append")
        assert update["delegation_id"] is None
        assert update["content"] == "Ask when the office opens."
    finally:
        await provider.close()


async def test_external_context_is_not_merged_into_recipient_transcript(live_server):
    provider = make_provider(live_server)
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await send(live_server, {"type": "session.input_transcript.delta", "delta": "Thursday"})
        await next_kind(provider, "transcript")
        await provider.update_context("The agent requests confirmation.")
        for fragment in ("No", ", Friday."):
            await send(live_server, {"type": "session.input_transcript.delta", "delta": fragment})
            await next_kind(provider, "transcript")
        assert provider.history == [
            {"role": "user", "content": "Thursday"},
            {"role": "user", "content": "The agent requests confirmation."},
            {"role": "user", "content": "No, Friday."},
        ]
    finally:
        await provider.close()


async def test_continuous_menu_allows_delayed_dtmf(live_server):
    entered, release = asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not kwargs["allow_dtmf"]:
            return ""
        entered.set()
        await release.wait()
        await execute({"call_id": "key", "name": "send_dtmf", "arguments": '{"digits":"2"}'})
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await send(
            live_server,
            {
                "type": "session.input_transcript.delta",
                "delta": "For hours press two. For returns press three.",
            },
        )
        await delegate(live_server)
        await asyncio.wait_for(entered.wait(), 1)
        await send(
            live_server,
            {"type": "session.input_transcript.delta", "delta": " For hours press two."},
        )
        async with asyncio.timeout(1):
            while provider.revision < 2:
                await asyncio.sleep(0)
        release.set()
        event = await next_kind(provider, "tool")
        assert event["revision"] < provider.revision
        assert provider.validate_operation("send_dtmf", event)
        provider.reserve_dtmf()
        provider.complete_dtmf({"ok": True})
        await provider.tool_result("key", {"ok": True})
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "verdict,expected",
    [("menu", "ready"), ("information", "listening"), ("waiting", "awaiting_feedback")],
)
async def test_dtmf_feedback_requires_positive_evidence(live_server, verdict, expected):
    provider = make_provider(live_server)
    provider.backend.review_dtmf.return_value = verdict
    provider.dtmf_quiet = 0
    provider.dtmf_window = 0.001
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    provider.dtmf_feedback = "For all other departments press five."
    await provider.review_dtmf()
    assert provider.dtmf_state == expected


async def test_dtmf_silence_expires_without_model_request(live_server):
    provider = make_provider(live_server)
    provider.dtmf_feedback_timeout = 0
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    await provider.review_dtmf()
    assert provider.dtmf_state == "uncertain"
    provider.backend.review_dtmf.assert_not_awaited()


async def test_feedback_cannot_unlock_after_task_change(live_server):
    provider = make_provider(live_server)

    async def review(*args):
        provider.control_epoch += 1
        return "menu"

    provider.backend.review_dtmf.side_effect = review
    provider.dtmf_quiet = 0
    provider.dtmf_window = 0.001
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    provider.dtmf_feedback = "New menu"
    await provider.review_dtmf()
    assert provider.dtmf_state == "awaiting_feedback"


async def test_slow_feedback_review_is_bounded(live_server):
    provider = make_provider(live_server)

    async def slow(*args):
        await asyncio.Event().wait()

    provider.backend.review_dtmf.side_effect = slow
    provider.dtmf_quiet = 0
    provider.dtmf_feedback_timeout = 0.02
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    provider.dtmf_feedback = "Still speaking"
    await asyncio.wait_for(provider.review_dtmf(), 0.3)
    assert provider.dtmf_state == "uncertain"


def test_dtmf_exception_does_not_relax_completion_or_delegation():
    provider = OpenAILive(ProviderConfig(model="gpt-live-1"), "fake")
    snapshot = {"revision": 0, "control_epoch": 0, "dtmf_epoch": 0, "delegation_id": None}
    provider.revision = 3
    provider.menu_text = "Test menu"
    assert provider.validate_operation("send_dtmf", snapshot)
    assert not provider.validate_operation("finish_task", snapshot)
    assert not provider.validate_operation("hangup", snapshot)
    provider.delegation_id = "replacement"
    assert not provider.validate_operation("send_dtmf", snapshot)


async def test_menu_transcript_triggers_luna_without_live_delegation(live_server):
    opened = asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not history:
            opened.set()
            return "confirm today's closing time"
        await execute({"call_id": "menu-key", "name": "send_dtmf", "arguments": '{"digits":"2"}'})
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opened.wait(), 1)
        await asyncio.sleep(0.05)
        await send(
            live_server, {"type": "session.input_transcript.delta", "delta": "For hours press two."}
        )
        await next_kind(provider, "transcript")
        event = await next_kind(provider, "tool")
        assert event["name"] == "send_dtmf"
        assert provider.delegation_id is None
        backend.review_menu.assert_awaited_once()
    finally:
        await provider.close()


async def test_information_does_not_rearm_dtmf_even_with_new_epoch(live_server):
    provider = make_provider(live_server)
    provider.backend.review_dtmf.return_value = "information"
    provider.dtmf_quiet = 0
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    provider.dtmf_feedback = "Our hours are as follows. Executive members only..."
    await provider.review_dtmf()
    snapshot = {
        "revision": provider.revision,
        "control_epoch": 0,
        "dtmf_epoch": provider.dtmf_epoch,
        "delegation_id": None,
    }
    assert not provider.validate_operation("send_dtmf", snapshot)
    assert provider.validate_operation("finish_task", snapshot)


async def test_information_keeps_lock_until_complete_new_menu(live_server):
    provider = make_provider(live_server)
    provider.dtmf_quiet = 0
    provider.reserve_dtmf()
    provider.complete_dtmf({"ok": True})
    provider.backend.review_dtmf.side_effect = ["information", "waiting", "menu"]
    provider.dtmf_feedback = "Our hours are as follows."
    await provider.review_dtmf()
    assert provider.dtmf_state == "listening"
    provider.dtmf_feedback += " For another department press"
    await provider.review_dtmf()
    assert provider.dtmf_state == "listening"
    provider.dtmf_feedback += " two."
    await provider.review_dtmf()
    assert provider.dtmf_state == "ready"
    assert provider.backend.review_dtmf.call_args.args[1] == " For another department press two."


async def test_nonstop_transcript_batches_have_bounded_wait(live_server):
    opened, evaluated = asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if history:
            evaluated.set()
        else:
            opened.set()
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.backend_quiet = 0.3
    provider.backend_window = 0.15
    producer = None
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opened.wait(), 1)

        async def stream():
            for _ in range(40):
                await send(
                    live_server, {"type": "session.input_transcript.delta", "delta": " menu"}
                )
                await asyncio.sleep(0.02)

        producer = asyncio.create_task(stream())
        await asyncio.wait_for(evaluated.wait(), 0.6)
        assert not producer.done()  # Luna can assess continuous input without Live delegation.
    finally:
        if producer:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        await provider.close()


async def test_dtmf_waits_for_luna_to_confirm_complete_menu(live_server):
    opening = asyncio.Event()
    snapshots = []
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not history:
            opening.set()
            return ""
        snapshots.append(kwargs)
        if kwargs["allow_dtmf"]:
            await execute(
                {"call_id": "right-key", "name": "send_dtmf", "arguments": '{"digits":"2"}'}
            )
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.backend_quiet = provider.backend_window = 0.01
    first = "To reach administrative staff press one."
    rest = " For warehouse hours press two. For pharmacy press four."
    backend.review_menu.side_effect = lambda text, *args: (
        "menu" if text.count(first) > 1 else "waiting"
    )
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opening.wait(), 1)
        for part in [first, " For warehouse hours press two.", " For pharmacy press four."]:
            await send(live_server, {"type": "session.input_transcript.delta", "delta": part})
            await asyncio.sleep(0.08)
            assert not snapshots  # Even the relevant complete option is not a whole menu.
        await asyncio.sleep(0.25)
        assert not snapshots
        assert backend.review_menu.await_count >= 1
        await send(live_server, {"type": "session.input_transcript.delta", "delta": " " + first})
        event = await next_kind(provider, "tool")
        assert event["call_id"] == "right-key"
        assert snapshots[0]["menu_text"] == first + rest + " " + first
        assert provider.delegation_id is None
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "text",
    [
        "请按照说明回答，按时营业。",
        "Please dial me tomorrow.",
        "Press enquiries are handled by our office.",
    ],
)
async def test_ordinary_dialogue_keywords_never_start_menu_wait(live_server, text):
    opened, answered = asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not history:
            opened.set()
        else:
            assert history[-1]["content"] == text
            answered.set()
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.backend_quiet = provider.backend_window = 0.01
    backend.review_menu.return_value = "dialogue"
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opened.wait(), 1)
        await send(live_server, {"type": "session.input_transcript.delta", "delta": text})
        await next_kind(provider, "transcript")
        await asyncio.wait_for(answered.wait(), 0.6)
        assert not provider.menu_text
        assert provider.delegation_id is None
        assert provider.dtmf_state == "ready"
    finally:
        await provider.close()


async def test_dtmf_feedback_is_assessed_without_live_delegation(live_server):
    opened = asyncio.Event()
    backend = AsyncMock()

    async def run(*args, **kwargs):
        opened.set()
        return ""

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.backend_quiet = provider.backend_window = 0.01
    provider.dtmf_feedback_timeout = 0.3
    backend.review_dtmf.return_value = "waiting"
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opened.wait(), 1)
        provider.reserve_dtmf()
        provider.complete_dtmf({"ok": True})
        await send(
            live_server,
            {
                "type": "session.input_transcript.delta",
                "delta": "For hours press one. For pharmacy",
            },
        )
        await next_kind(provider, "dtmf_no_progress")
        backend.review_dtmf.assert_awaited_once()
        assert provider.delegation_id is None
        assert backend.run.await_count == 1
    finally:
        await provider.close()


async def test_new_partial_menu_blocks_superseded_dialogue_retry(live_server):
    opened, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def run(history, operations, execute, **kwargs):
        if not history:
            opened.set()
            return ""
        entered.set()
        await release.wait()
        return "outdated answer"

    backend.run.side_effect = run
    provider = make_provider(live_server, backend)
    provider.backend_quiet = provider.backend_window = 0.01
    backend.review_menu.side_effect = lambda text, *args: (
        "waiting" if "press" in text else "dialogue"
    )
    try:
        await provider.open("task", TOOLS)
        await provider.start_response()
        await asyncio.wait_for(opened.wait(), 1)
        await send(live_server, {"type": "session.input_transcript.delta", "delta": "Hello."})
        await delegate(live_server)
        await asyncio.wait_for(entered.wait(), 1)
        await send(
            live_server,
            {"type": "session.input_transcript.delta", "delta": " For hours press one."},
        )
        async with asyncio.timeout(1):
            while provider.revision < 2:
                await asyncio.sleep(0)
        release.set()
        await next_kind(provider, "backend_stale")
        await asyncio.sleep(0.25)
        assert backend.run.await_count == 2
        assert not any(e["type"] == "session.commentary.append" for e in live_server[1])
    finally:
        await provider.close()


async def test_menu_assessment_reuses_unchanged_input_and_rejects_old_context(live_server):
    provider = make_provider(live_server)
    provider.menu_buffer = "For hours press two. Repeat these options."
    assert await provider.assess_menu() == "menu"
    assert await provider.assess_menu() == "menu"
    provider.backend.review_menu.assert_awaited_once()

    async def changed(*args):
        provider.control_epoch += 1
        return "menu"

    provider.backend.review_menu.side_effect = changed
    provider.menu_buffer += " More text."
    assert await provider.assess_menu() == "waiting"
    assert not provider.menu_text


@pytest.mark.parametrize("verdict", ["menu", "information"])
async def test_confirmed_menu_repetition_preserves_completion_but_correction_invalidates(
    live_server, verdict
):
    provider = make_provider(live_server)
    provider.backend.run.return_value = ""
    provider.backend.review_menu.return_value = verdict
    menu = "For current warehouse hours press one. For address and directions press two. For holidays press three. "
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await send(live_server, {"type": "session.input_transcript.delta", "delta": menu})
        await next_kind(provider, "transcript")
        provider.menu_buffer = menu
        await provider.assess_menu()
        # Establish a repeated cycle; subsequent exact continuations add no new facts.
        await send(live_server, {"type": "session.input_transcript.delta", "delta": menu[:75]})
        await next_kind(provider, "transcript")
        revision = provider.revision
        await send(live_server, {"type": "session.input_transcript.delta", "delta": menu[75:]})
        repeated = await next_kind(provider, "transcript")
        assert repeated["repeated_recording"] and provider.revision == revision
        await send(
            live_server,
            {"type": "session.input_transcript.delta", "delta": "Actually we close at six today."},
        )
        corrected = await next_kind(provider, "transcript")
        assert not corrected["repeated_recording"] and provider.revision > revision
        provider.reserve_dtmf()
        assert not provider.repeat_source
    finally:
        await provider.close()


async def test_recorded_answer_does_not_send_dialogue_guidance_to_live(live_server):
    provider = make_provider(live_server)
    provider.backend.review_menu.return_value = "information"
    evaluated = asyncio.Event()

    async def run(history, operations, execute, **kwargs):
        if history:
            assert kwargs["recorded_answer"] and not kwargs["allow_dtmf"]
            evaluated.set()
            return "Please press two or ask the recording for confirmation."
        return ""

    provider.backend.run.side_effect = run
    try:
        await provider.open("hours", TOOLS)
        await provider.start_response()
        await send(
            live_server,
            {
                "type": "session.input_transcript.delta",
                "delta": "The warehouse is closed. Weekday hours end at 8:30 PM.",
            },
        )
        await asyncio.wait_for(evaluated.wait(), 2)
        await asyncio.sleep(0.02)
        assert not any(e["type"] == "session.commentary.append" for e in live_server[1])
    finally:
        await provider.close()
