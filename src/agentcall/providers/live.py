"""GPT-Live continuous voice with application-owned, revisioned delegation.

Protocol: https://developers.openai.com/api/docs/guides/live-delegation
No Realtime turn, truncate, or response-completion events are used here.
"""

import asyncio
import base64
import copy
import re
from contextlib import suppress

import websockets
from websockets.exceptions import WebSocketException

from agentcall.providers.base import ProviderError
from agentcall.providers.live_backend import LiveBackend
from agentcall.providers.openai import OpenAIRealtime


class SupersededWork(Exception):
    """Stop further reasoning steps; never cancel an already running operation."""


def live_tools(tools):
    result = copy.deepcopy(tools)
    closing = next(t for t in result if t["name"] == "hangup")
    closing["description"] = (
        "Schedule a brief rendered farewell and hangup after playback. Only after finish_task. "
        "The application cancels pending hangup if the recipient speaks. Do not speak goodbye twice."
    )
    closing["parameters"]["properties"]["farewell"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "description": "Only a brief natural thank-you and goodbye in the conversation language.",
    }
    closing["parameters"]["required"].append("farewell")
    finish = next(t for t in result if t["name"] == "finish_task")
    finish["description"] = (
        "Submit status and structured result only after all required spoken exchanges "
        "and recipient replies. Supply a short farewell in the conversation language. "
        "Report unmet requirements honestly. On success the "
        "application plays it and hangs up automatically; do not call hangup separately."
    )
    finish["parameters"]["properties"]["farewell"] = copy.deepcopy(
        closing["parameters"]["properties"]["farewell"]
    )
    finish["parameters"]["required"].append("farewell")
    return result


class OpenAILive(OpenAIRealtime):
    continuous_audio = True

    def __init__(
        self,
        config,
        api_key,
        *,
        endpoint="wss://api.openai.com/v1/live/sessions",
        timeout=20,
        backend=None,
    ):
        super().__init__(config, api_key, endpoint=endpoint, timeout=timeout)
        self.conversation_instructions = "Be a concise AI telephone assistant. Delegate task work."
        self.backend = backend
        self.queue = asyncio.Queue(maxsize=256)
        self.history = []
        self._last_transcript = None
        self.operations = []
        self.revision = 0
        self.input_dirty_since = None
        self.last_input_at = 0.0
        self.backend_quiet = 0.5
        self.backend_window = 3.0
        self.feedback_reviewed = 0
        self.feedback_boundary = 0
        self.menu_buffer = ""
        self.menu_text = ""
        self.backend_requested = False
        self.menu_assessment = None
        self.repeat_source = ""
        self.control_epoch = 0
        self.dtmf_epoch = 0
        self.dtmf_state = "ready"
        self.dtmf_feedback = ""
        self.dtmf_before = []
        self.dtmf_last_input = 0.0
        self.dtmf_deadline = 0.0
        self.dtmf_feedback_timeout = 45.0
        self.dtmf_quiet = 0.5
        self.dtmf_window = 3.0
        self.pending_tools = {}
        self.reader = None
        self.worker = None
        self.work = asyncio.Event()
        self.delegation_id = None
        self.seen_delegations = set()
        self.closed = asyncio.Event()
        self.started = False
        self._playback_blocked = False
        self.final_usage = None
        self.input_ready = asyncio.Event()
        self.input_queue = asyncio.Queue(maxsize=4096)
        self.input_bytes = 0
        self.sender = None
        self.audio_generation = 0

    @property
    def playback_blocked(self):
        return self._playback_blocked

    @playback_blocked.setter
    def playback_blocked(self, value):
        if value != self._playback_blocked:
            self.audio_generation += 1
        self._playback_blocked = value

    async def open(self, instructions, tools):
        # Prepare before dialing, but create the speaking session only after the phone answers.
        if not self.api_key:
            raise ProviderError("OpenAI API key environment variable is not configured")
        self.tools = live_tools(tools)
        self.backend = self.backend or LiveBackend(self.api_key, instructions, self.tools)

    async def start_response(self):
        # Provider interface startup hook, never a Live response.create speech trigger.
        if self.started:
            return
        try:
            self.ws = await websockets.connect(
                self.endpoint,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                open_timeout=self.timeout,
                close_timeout=2,
                max_size=4 * 1024 * 1024,
                max_queue=8,
            )
            await self.send(
                {
                    "type": "session.start",
                    "session": {
                        "model": self.config.model,
                        "instructions": self.conversation_instructions,
                        "audio": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "output": {"voice": self.config.voice},
                        },
                        "delegation": {"type": "client"},
                        "input": [
                            {
                                "type": "message",
                                "role": "developer",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "The phone has answered. The backend is preparing the call purpose. "
                                        f"In {self.config.language}, use the opening template from your "
                                        "instructions once that purpose arrives, even if the recipient is silent. "
                                        "Then pause and listen; do not append the first task question. "
                                        "Yield if the recipient speaks, and address them before continuing.",
                                    }
                                ],
                            }
                        ],
                    },
                }
            )
            async with asyncio.timeout(self.timeout):
                while True:
                    event = await self.receive()
                    if event.get("type") == "error":
                        raise ProviderError("OpenAI rejected Live session configuration")
                    if event.get("type") == "session.started":
                        fmt = event.get("session", {}).get("audio", {}).get("format", {})
                        if fmt != {"type": "audio/pcm", "rate": 24000}:
                            raise ProviderError("OpenAI Live resolved unsupported audio format")
                        break
            self.started = True
            self.input_ready.set()
            self.reader = asyncio.create_task(self.read_live())
            self.worker = asyncio.create_task(self.run_backend())
            # The voice session has no task goal: supply its first step without
            # waiting for the recipient to ask for an unknown task.
            self.backend_requested = True
            self.work.set()
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise ProviderError("OpenAI Live connection failed or timed out") from exc

    async def send_audio(self, pcm):
        if len(pcm) % 2:
            raise ValueError("PCM must contain complete 16-bit samples")
        if not pcm or self.closing:
            return
        # Capture from answer onward, including session startup. Bound startup/network
        # delay explicitly instead of losing the first words in the SCO socket buffer.
        if self.input_bytes + len(pcm) > self.input_rate * 2 * 2:
            raise ProviderError("Live input audio backlog exceeded two seconds")
        self.input_queue.put_nowait(pcm)
        self.input_bytes += len(pcm)
        if self.sender is None:
            self.sender = asyncio.create_task(self.send_input())

    async def send_input(self):
        try:
            await self.input_ready.wait()
            deadline = asyncio.get_running_loop().time()
            while not self.closing:
                pcm = await self.input_queue.get()
                # Pace buffered audio at its recorded rate, as required by Live.
                for offset in range(0, len(pcm), 960):
                    chunk = pcm[offset : offset + 960]
                    now = asyncio.get_running_loop().time()
                    deadline = max(deadline, now - 0.02)
                    await self.send(
                        {
                            "type": "session.input_audio.append",
                            "audio": base64.b64encode(chunk).decode(),
                        }
                    )
                    self.input_bytes -= len(chunk)
                    deadline += len(chunk) / (self.input_rate * 2)
                    await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.queue.put(ProviderError("Live input stream failed"))

    async def append(self, kind, content, delegation_id=None):
        # UTF-8 bytes conservatively bound tokens. Never split a fact or instruction
        # into independently injected fragments (which can reverse its meaning).
        if not isinstance(content, str) or len(content.encode("utf-8")) > 500:
            raise ProviderError(
                "Live context update must be a coherent summary of at most 500 UTF-8 bytes"
            )
        if content:
            await self.send(
                {
                    "type": f"session.{kind}.append",
                    "delegation_id": delegation_id,
                    "content": content,
                }
            )

    async def update_context(self, text):
        self.control_epoch += 1
        self.revision += 1
        self.history.append({"role": "user", "content": text})
        await self.queue.put({"kind": "live_context", "revision": self.revision})
        await self.append(
            "thinking", "The task context was updated. The backend is reviewing the new request."
        )
        self.backend_requested = True
        self.work.set()

    async def truncate_audio(self, item_id, content_index, audio_end_ms):
        raise ProviderError("Live has no item truncation; use local playback control")

    async def read_live(self):
        try:
            while True:
                event = await self.receive()
                kind = event.get("type")
                if self.closing and kind not in ("session.closed", "error"):
                    continue
                if kind == "session.output_audio.delta":
                    try:
                        pcm = base64.b64decode(event["delta"], validate=True)
                        if len(pcm) % 2:
                            raise ValueError("odd PCM length")
                    except (ValueError, KeyError) as exc:
                        raise ProviderError("OpenAI Live sent invalid PCM audio") from exc
                    if not self.playback_blocked and not self.closing:
                        self.queue.put_nowait(
                            {"kind": "audio", "pcm": pcm, "generation": self.audio_generation}
                        )
                elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                    role = "user" if kind == "session.input_transcript.delta" else "assistant"
                    text = event.get("delta", "")
                    if not isinstance(text, str):
                        raise ProviderError("Invalid Live transcript")
                    if not text:
                        continue
                    if (
                        self.history
                        and self.history[-1] is self._last_transcript
                        and self._last_transcript["role"] == role
                    ):
                        self._last_transcript["content"] += text
                    else:
                        self._last_transcript = {"role": role, "content": text}
                        self.history.append(self._last_transcript)
                    if role == "user":
                        # Conservative invalidation is not cancellation: in-flight actions finish
                        # and enter the ledger, but future actions must use the latest context.
                        repeated_recording = self.is_recording_repeat(text)
                        if not repeated_recording:
                            self.revision += 1
                        now = asyncio.get_running_loop().time()
                        if self.input_dirty_since is None:
                            self.input_dirty_since = now
                        self.last_input_at = now
                        self.menu_buffer += text
                        # Luna assesses accumulated input independently of Live delegation.
                        self.backend_requested = True
                        self.work.set()
                        if self.dtmf_state in {"sending", "awaiting_feedback", "listening"}:
                            self.dtmf_feedback += text
                            self.dtmf_last_input = asyncio.get_running_loop().time()
                    await self.queue.put(
                        {
                            "kind": "transcript",
                            "role": role,
                            "text": text,
                            "delta": True,
                            "start_ms": event.get("start_ms"),
                            "end_ms": event.get("end_ms"),
                            "revision": self.revision,
                            "repeated_recording": repeated_recording if role == "user" else False,
                        }
                    )
                elif kind == "session.delegation.created":
                    delegation = event.get("delegation", {})
                    ident = delegation.get("id")
                    if delegation.get("target") != "client" or not isinstance(ident, str):
                        raise ProviderError("Unexpected Live delegation")
                    if ident not in self.seen_delegations:
                        self.seen_delegations.add(ident)
                        self.delegation_id = ident
                        self.backend_requested = True
                        self.work.set()
                        await self.queue.put(
                            {
                                "kind": "delegation",
                                "delegation_id": ident,
                                "revision": self.revision,
                                "offset_ms": event.get("offset_ms"),
                            }
                        )
                elif kind == "session.closed":
                    self.final_usage = event.get("usage")
                    self.closed.set()
                    if not self.closing:
                        raise ProviderError("Live session closed before the call ended")
                    return
                elif kind == "error":
                    raise ProviderError("OpenAI Live rejected a command or failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.queue.put(
                exc if isinstance(exc, ProviderError) else ProviderError("Live event stream failed")
            )

    def validate_operation(self, name, snapshot):
        if self.closing or snapshot.get("delegation_id") != self.delegation_id:
            return False
        if name == "send_dtmf":
            return (
                snapshot.get("control_epoch") == self.control_epoch
                and snapshot.get("dtmf_epoch") == self.dtmf_epoch
                and self.dtmf_state == "ready"
                and bool(self.menu_text)
            )
        return snapshot.get("revision") == self.revision

    def is_recording_repeat(self, text):
        # Exact continuation of a previously heard, Luna-confirmed recording only.
        # Novel words (including corrections) still invalidate pending work.
        def normalize(value):
            return re.sub(r"[\W_]+", " ", value.casefold())

        before = normalize(self.menu_buffer)
        anchor = before[-64:]
        if len(anchor) < 64 or not self.repeat_source:
            return False
        continuation = normalize(self.menu_buffer + text)[len(before) :]
        return anchor + continuation in normalize(self.repeat_source)

    async def assess_menu(self):
        text = self.menu_buffer
        stamp = (self.control_epoch, self.dtmf_epoch, text)
        if self.menu_assessment and self.menu_assessment[0] == stamp:
            return self.menu_assessment[1]
        self.menu_text = ""
        try:
            async with asyncio.timeout(10):
                verdict = await self.backend.review_menu(
                    text,
                    copy.deepcopy(self.history),
                    copy.deepcopy(self.operations),
                )
        except TimeoutError:
            verdict = "waiting"
        if stamp[:2] != (self.control_epoch, self.dtmf_epoch) or self.closing:
            return "waiting"
        if verdict not in {"menu", "information", "dialogue"}:
            verdict = "waiting"
        self.menu_assessment = (stamp, verdict)
        if verdict == "menu":
            self.menu_text = text
        if verdict in {"menu", "information"}:
            self.repeat_source = text
        elif verdict == "dialogue":
            self.repeat_source = ""
        await self.queue.put(
            {
                "kind": "ivr_assessment",
                "verdict": verdict,
                "input_chars": len(text),
                "dtmf_epoch": self.dtmf_epoch,
            }
        )
        return verdict

    def reserve_dtmf(self):
        # Called under the task's tool lock, with no await after validation.
        self.dtmf_epoch += 1
        self.dtmf_state = "sending"
        self.menu_buffer, self.menu_text = "", ""
        self.menu_assessment = None
        self.repeat_source = ""
        self.dtmf_before = copy.deepcopy(self.history)
        self.dtmf_feedback = ""
        self.feedback_reviewed = 0
        self.feedback_boundary = 0

    def complete_dtmf(self, result):
        self.dtmf_state = "awaiting_feedback" if result.get("ok") else "uncertain"
        now = asyncio.get_running_loop().time()
        self.dtmf_last_input = now
        self.dtmf_deadline = now + self.dtmf_feedback_timeout
        self.work.set()

    async def review_dtmf(self):
        # Only the single backend worker reviews feedback; never run tools here.
        loop = asyncio.get_running_loop()
        listening = self.dtmf_state == "listening"
        deadline = loop.time() + 5.0 if listening else self.dtmf_deadline
        window = loop.time() if listening else min(loop.time() + self.dtmf_window, deadline)
        while loop.time() < window:
            if self.dtmf_feedback and loop.time() - self.dtmf_last_input >= self.dtmf_quiet:
                break
            await asyncio.sleep(min(0.05, max(0, window - loop.time())))
        control, delegation = self.control_epoch, self.delegation_id
        verdict = "waiting"
        feedback = self.dtmf_feedback[self.feedback_boundary :] if listening else self.dtmf_feedback
        reviewed = len(self.dtmf_feedback)
        if feedback:
            try:
                async with asyncio.timeout(max(0.001, deadline - loop.time())):
                    verdict = await self.backend.review_dtmf(
                        copy.deepcopy(self.dtmf_before),
                        feedback,
                        copy.deepcopy(self.operations),
                    )
            except TimeoutError:
                verdict = "waiting"
        if control != self.control_epoch or delegation != self.delegation_id:
            verdict = "waiting"
        await self.queue.put({"kind": "dtmf_feedback", "verdict": verdict})
        if verdict == "menu":
            self.dtmf_state = "ready"
            return
        if verdict == "information" or listening:
            # Information playback is progress, not permission to send another key.
            self.dtmf_state = "listening"
            if not listening:
                self.feedback_boundary = reviewed
            self.feedback_reviewed = reviewed
            return
        if loop.time() >= self.dtmf_deadline:
            self.dtmf_state = "uncertain"
        else:
            # Bound retries even when a complete but unchanged menu is already buffered.
            await asyncio.sleep(min(self.dtmf_window, max(0, self.dtmf_deadline - loop.time())))

    async def run_backend(self):
        try:
            while not self.closing:
                await self.work.wait()
                self.work.clear()
                # Bound batching even for nonstop menus, using one serial worker.
                await asyncio.sleep(0.15)
                while self.input_dirty_since is not None:
                    now = asyncio.get_running_loop().time()
                    delay = (
                        min(
                            self.last_input_at + self.backend_quiet,
                            self.input_dirty_since + self.backend_window,
                        )
                        - now
                    )
                    if delay <= 0:
                        break
                    await asyncio.sleep(min(0.05, delay))
                self.input_dirty_since = None
                self.work.clear()
                if self.dtmf_state == "awaiting_feedback":
                    await self.review_dtmf()
                    self.work.set()
                    continue
                if self.dtmf_state == "uncertain":
                    await self.queue.put({"kind": "dtmf_no_progress"})
                    return
                if not self.backend_requested:
                    continue
                if (
                    self.dtmf_state == "listening"
                    and len(self.dtmf_feedback) > self.feedback_reviewed
                ):
                    await self.review_dtmf()
                self.backend_requested = False
                verdict = None
                if self.menu_buffer and self.dtmf_state == "ready":
                    verdict = await self.assess_menu()
                    if verdict == "waiting":
                        continue
                revision, delegation = self.revision, self.delegation_id
                snapshot = {
                    "revision": revision,
                    "delegation_id": delegation,
                    "control_epoch": self.control_epoch,
                    "dtmf_epoch": self.dtmf_epoch,
                }
                opening_request = not self.history and delegation is None
                dtmf_attempted = False

                async def execute(
                    item, revision=revision, delegation=delegation, snapshot=snapshot
                ):
                    nonlocal dtmf_attempted
                    if (
                        self.closing
                        or snapshot["control_epoch"] != self.control_epoch
                        or delegation != self.delegation_id
                    ):
                        raise SupersededWork
                    if item["name"] == "send_dtmf" and (
                        self.dtmf_state == "listening" or not self.menu_text
                    ):
                        return {
                            "ok": False,
                            "state": "rejected",
                            "error": "Wait for a complete menu; do not interrupt information playback.",
                        }
                    if dtmf_attempted or not self.validate_operation(item["name"], snapshot):
                        raise SupersededWork
                    if item["name"] == "send_dtmf":
                        dtmf_attempted = True
                    call_id = item["call_id"]
                    previous = next(
                        (op for op in self.operations if op["call_id"] == call_id), None
                    )
                    if previous is not None:
                        return previous["result"]
                    if call_id in self.pending_tools:
                        raise ProviderError("Backend reused a pending operation ID")
                    future = asyncio.get_running_loop().create_future()
                    self.pending_tools[call_id] = (future, item, revision)
                    await self.queue.put(
                        {
                            "kind": "tool",
                            "call_id": call_id,
                            "name": item["name"],
                            "arguments": item["arguments"],
                            **snapshot,
                        }
                    )
                    try:
                        result = await future
                        if item["name"] != "send_dtmf" and (
                            revision != self.revision or delegation != self.delegation_id
                        ):
                            raise SupersededWork
                        return result
                    finally:
                        self.pending_tools.pop(call_id, None)

                try:
                    text = await self.backend.run(
                        copy.deepcopy(self.history),
                        copy.deepcopy(self.operations),
                        execute,
                        allow_dtmf=self.dtmf_state == "ready" and bool(self.menu_text),
                        menu_text=self.menu_text,
                        recorded_answer=verdict == "information" or self.dtmf_state == "listening",
                    )
                except SupersededWork:
                    text = ""
                if self.closing:
                    return
                if dtmf_attempted:
                    self.work.set()
                    continue
                if (
                    delegation != self.delegation_id
                    or snapshot["control_epoch"] != self.control_epoch
                    or (revision != self.revision and not opening_request)
                ):
                    await self.queue.put(
                        {
                            "kind": "backend_stale",
                            "revision": revision,
                            "current_revision": self.revision,
                        }
                    )
                    # New input will pass through Luna assessment before task reasoning.
                    self.backend_requested = True
                    self.work.set()
                    continue
                if (
                    text
                    and not self.playback_blocked
                    and verdict != "information"
                    and self.dtmf_state != "listening"
                ):
                    await self.append("commentary", text, delegation)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.queue.put(
                ProviderError("Live backend work failed; no automatic action retry")
            )

    async def tool_result(self, call_id, result):
        pending = self.pending_tools.get(call_id)
        if pending is None:
            raise ProviderError("Unknown Live operation result")
        future, item, revision = pending
        record = {
            "call_id": call_id,
            "name": item["name"],
            "arguments": item["arguments"],
            "revision": revision,
            "result": result,
        }
        self.operations.append(record)
        if not future.done():
            future.set_result(result)

    async def events(self):
        while not self.closing:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self):
        self.closing = True
        for job in (self.worker, self.sender):
            if job:
                job.cancel()
        await asyncio.gather(
            *(job for job in (self.worker, self.sender) if job), return_exceptions=True
        )
        try:
            if self.started and self.ws and not self.closed.is_set():
                await self.send({"type": "session.close"})
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.closed.wait(), 2)
        finally:
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            try:
                if self.ws:
                    await self.ws.close()
            finally:
                if self.backend:
                    await self.backend.close()
