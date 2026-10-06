import asyncio
import json
import os
import re
from contextlib import suppress

from jsonschema import Draft202012Validator, ValidationError

from agentcall.audio.bridge import AudioBridge
from agentcall.bluetooth.hfp import HFPError
from agentcall.providers.base import ProviderError
from agentcall.providers.gemini import GeminiLive
from agentcall.providers.openai import OpenAIRealtime
from agentcall.storage.store import now
from agentcall.tasks.models import TOOLS, ProviderConfig, TaskInput, instructions
from agentcall.vendor import at


class TaskFailure(Exception):
    def __init__(self, outcome, message):
        self.outcome = outcome
        super().__init__(message)


class TaskManager:
    def __init__(self, backend, config, provider_factory=None):
        self.backend = backend
        self.config = config
        self.store = backend.store
        self.store.init_tasks()
        self.factory = provider_factory or self.make_provider
        self.runs = {}
        self.scheduler = None
        self.wake = asyncio.Event()
        self.closing = False
        self.backend.listeners.add(self.on_event)

    def make_provider(self, config):
        env = (
            self.config.api_key_env
            if config.provider == "openai"
            else self.config.gemini_api_key_env
        )
        key = os.environ.get(env, "")
        if not key:
            raise ProviderError(f"{config.provider} API key environment variable is not configured")
        provider = OpenAIRealtime if config.provider == "openai" else GeminiLive
        return provider(config, key, timeout=self.config.model_connect_seconds)

    def on_event(self, event):
        self.wake.set()

    async def start(self):
        self.store.recover_tasks()
        self.scheduler = asyncio.create_task(self.schedule())
        self.wake.set()

    def create(self, body: TaskInput):
        path = self.backend.path(body.device)
        data = body.model_dump(mode="json")
        data["device"] = path
        if body.contact_id:
            contacts = [c for c in self.store.contacts(device=path) if c["id"] == body.contact_id]
            if not contacts:
                raise ValueError("contact does not belong to this device")
            data["number"] = re.sub(r"[\s().-]", "", contacts[0]["number"])
        at.cmd_dial(data["number"])
        base = self.config.provider.model_dump()
        override = body.config.model_dump(exclude_none=True)
        if override.get("provider", base["provider"]) != base["provider"]:
            defaults = ProviderConfig(provider=override["provider"]).model_dump()
            base = {**defaults, "language": base["language"]}
        options = {**base["options"], **override.pop("options", {})}
        resolved = ProviderConfig(**{**base, **override, "options": options}).model_dump()
        task_id = self.store.create_task(path, data, resolved)
        self.event(task_id, "task.created")
        if body.start_immediately:
            self.start_task(task_id)
        return self.store.task(task_id)

    def start_task(self, task_id, key=None):
        if key is not None and (not key.strip() or len(key) > 256):
            raise ValueError("Idempotency-Key must be 1–256 nonblank characters")
        task = self.store.start_task(task_id, key)
        self.wake.set()
        return task

    def event(self, task_id, event_name, **data):
        if "kind" in data:
            data["provider_event"] = data.pop("kind")
        task = self.store.task(task_id)
        if event_name in ("tool.call", "tool.result") and "call_id" in data:
            data["tool_call_id"] = data.pop("call_id")
        data.setdefault("device", task["device"])
        if task["call_id"]:
            data.setdefault("call_id", task["call_id"])
        self.store.task_event(task_id, event_name, data)
        self.backend.emit(event_name, task_id=task_id, **data)

    def state(self, task_id, state, **data):
        self.store.update_task(task_id, state=state, **data)
        self.event(task_id, "task.state", state=state)

    async def schedule(self):
        try:
            while not self.closing:
                self.wake.clear()
                for task_id in self.store.task_ids("queued"):
                    task = self.store.task(task_id)
                    device = task["device"]
                    if device in self.backend.claims or device in self.backend.current:
                        continue
                    self.backend.claims[device] = task_id
                    run = TaskRun(self, task)
                    self.runs[task_id] = run
                    run.worker = asyncio.create_task(run.execute())
                    run.worker.add_done_callback(lambda worker, run=run: self.release(run, worker))
                try:
                    await asyncio.wait_for(self.wake.wait(), 0.1)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def release(self, run, worker):
        if self.backend.claims.get(run.device) == run.id:
            self.backend.claims.pop(run.device)
        self.runs.pop(run.id, None)
        if worker.cancelled() and self.store.task(run.id)["state"] != "ended":
            if run.cancel_reason != "service_stopped":
                self.state(run.id, "ended", outcome=run.cancel_reason, ended_at=now())
        self.wake.set()

    async def cancel(self, task_id):
        task = self.store.task(task_id)
        if task["state"] == "ended":
            return task
        run = self.runs.get(task_id)
        if run:
            run.cancel_reason = "user_cancelled"
            run.worker.cancel()
        else:
            self.state(task_id, "ended", outcome="user_cancelled", ended_at=now())
        self.wake.set()
        return self.store.task(task_id)

    async def update_context(self, task_id, text):
        run = self.runs.get(task_id)
        if not run or not run.bridge or run.done.is_set():
            raise HFPError("task conversation is not active")
        await run.provider.update_context(text)
        self.event(task_id, "task.context", text=text)

    async def close(self):
        self.closing = True
        self.backend.listeners.discard(self.on_event)
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        workers = [r.worker for r in self.runs.values()]
        for run in self.runs.values():
            run.cancel_reason = "service_stopped"
            run.worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


class TaskRun:
    def __init__(self, manager, task):
        self.manager = manager
        self.backend = manager.backend
        self.store = manager.store
        self.config = manager.config
        self.task = task
        self.id = task["id"]
        self.device = task["device"]
        self.worker = None
        self.provider = None
        self.bridge = None
        self.children = set()
        self.done = asyncio.Event()
        self.failure = None
        self.cancel_reason = "service_stopped"
        self.response_events = {}
        self.responses = set()
        self.interrupted_responses = set()
        self.continue_response = False
        self.hangup_job = None
        self.hangup_call_id = None
        self.hangup_requested = False
        self.audio_chunks = 0
        self.result_audio_floor = None
        self.last_audio_response = None
        self.opened = False

    def spawn(self, coro, outcome):
        task = asyncio.create_task(coro)
        self.children.add(task)

        def finished(t):
            if not t.cancelled() and t.exception():
                call = self.linked_call()
                if outcome == "audio_failed" and (
                    (self.hangup_requested and isinstance(t.exception(), OSError))
                    or (call and call["ended_at"])
                ):
                    # The phone ending a call closes SCO; that EOF is expected cleanup.
                    self.manager.wake.set()
                    return
                self.failure = TaskFailure(
                    "model_disconnected" if isinstance(t.exception(), ProviderError) else outcome,
                    str(t.exception()),
                )
                self.done.set()
                self.manager.wake.set()

        task.add_done_callback(finished)
        return task

    def linked_call(self):
        return self.store.task(self.id)["call"]

    def terminal(self):
        call = self.linked_call()
        if call and call["ended_at"]:
            reason = call["end_reason"]
            if reason in (
                "bluetooth_disconnected",
                "busy",
                "no_answer",
                "dial_failed",
                "service_restart",
            ):
                return reason
            if self.failure:
                return self.failure.outcome
            result = self.store.task(self.id)["model_result"]
            return result["status"] if result else "incomplete"
        if self.failure:
            return self.failure.outcome
        return None

    async def wait_connected(self):
        deadline = asyncio.get_running_loop().time() + self.config.answer_timeout_seconds
        while True:
            terminal = self.terminal()
            if terminal:
                raise TaskFailure(terminal, "call or model ended before conversation")
            call = self.linked_call()
            if call and call["state"] == "active":
                self.call_deadline = (
                    asyncio.get_running_loop().time() + self.task["input"]["max_call_seconds"]
                )
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TaskFailure("no_answer", "phone did not confirm answer before deadline")
            await asyncio.sleep(0.02)
        deadline = asyncio.get_running_loop().time() + self.config.audio_timeout_seconds
        while True:
            terminal = self.terminal()
            if terminal:
                raise TaskFailure(terminal, "call or model ended before audio became ready")
            if asyncio.get_running_loop().time() >= self.call_deadline:
                raise TaskFailure("timeout", "maximum call duration exceeded waiting for audio")
            audio = self.backend.audio.get(self.device)
            if audio and not audio.closed:
                if audio.owner:
                    raise TaskFailure("audio_failed", "SCO audio is owned by another client")
                audio.owner = True
                return audio
            if asyncio.get_running_loop().time() >= deadline:
                raise TaskFailure("audio_failed", "SCO audio readiness timeout")
            await asyncio.sleep(0.02)

    async def execute(self):
        outcome, error = "incomplete", None
        try:
            self.manager.state(self.id, "preparing", started_at=now())
            try:
                self.backend.slc(self.device)
            except HFPError as exc:
                raise TaskFailure("phone_unavailable", str(exc)) from exc
            try:
                self.provider = self.manager.factory(ProviderConfig(**self.task["config"]))
                await self.provider.open(instructions(self.task), TOOLS)
                self.opened = True
            except ProviderError as exc:
                raise TaskFailure("model_connection_failed", str(exc)) from exc
            self.spawn(self.read_provider(), "model_disconnected")
            if self.failure:
                raise self.failure
            self.manager.state(self.id, "dialing")
            try:
                await self.backend.dial(self.device, self.task["input"]["number"], owner=self.id)
            except HFPError as exc:
                raise TaskFailure(self.terminal() or "dial_failed", str(exc)) from exc
            audio = await self.wait_connected()
            self.bridge = AudioBridge(audio, self.provider)
            self.manager.state(self.id, "in_call")
            self.manager.event(self.id, "task.audio", **self.bridge.status())
            self.spawn(self.bridge.input_loop(), "audio_failed")
            self.manager.event(self.id, "task.opening_pause", seconds=1.0)
            self.spawn(self.bridge.output_loop(start_delay=1.0), "audio_failed")
            await self.provider.start_response()
            async with asyncio.timeout_at(self.call_deadline):
                while True:
                    terminal = self.terminal()
                    if terminal:
                        outcome = terminal
                        error = {"message": str(self.failure)} if self.failure else None
                        break
                    if self.done.is_set():
                        result = self.store.task(self.id)["model_result"]
                        outcome = result["status"] if result else "incomplete"
                        break
                    await asyncio.sleep(0.02)
        except TaskFailure as exc:
            outcome, error = exc.outcome, {"message": str(exc)}
        except TimeoutError:
            outcome, error = "timeout", {"message": "maximum call duration exceeded"}
        except asyncio.CancelledError:
            outcome = self.cancel_reason
        except ProviderError as exc:
            outcome, error = "model_disconnected", {"message": str(exc)}
        except Exception as exc:
            outcome, error = "execution_failed", {"type": type(exc).__name__, "message": str(exc)}
        finally:
            self.manager.state(self.id, "finalizing")
            for child in self.children:
                child.cancel()
            await asyncio.gather(*self.children, return_exceptions=True)
            try:
                await self.hangup_phone()
            except (HFPError, TimeoutError, OSError) as exc:
                self.manager.event(self.id, "task.cleanup_error", error=str(exc))
            if self.bridge:
                self.manager.event(self.id, "task.audio", **self.bridge.status())
                self.bridge.audio.owner = False
            if self.provider:
                with suppress(ProviderError, OSError, TimeoutError):
                    await self.provider.close()
            self.manager.state(self.id, "ended", outcome=outcome, error=error, ended_at=now())
            self.backend.claims.pop(self.device, None)
            self.manager.runs.pop(self.id, None)
            self.manager.wake.set()

    async def hangup_phone(self):
        call = self.linked_call()
        if not call or call["ended_at"] or self.backend.current.get(self.device) != call["id"]:
            return
        slc = self.backend.slc(self.device)
        async with asyncio.timeout(self.config.hangup_timeout_seconds):
            # An accepted ATD may not have emitted callsetup yet; still cancel that pending call.
            self.hangup_requested = True
            self.manager.event(self.id, "task.hangup_requested")
            await slc.command(at.cmd_hangup())
            while self.backend.current.get(self.device) == call["id"]:
                await asyncio.sleep(0.02)

    async def read_provider(self):
        async for event in self.provider.events():
            kind = event["kind"]
            if kind == "audio":
                if self.bridge is None:
                    raise ProviderError("model produced audio before phone/audio readiness")
                if event.get("response_id") in self.interrupted_responses:
                    continue
                await self.bridge.enqueue(
                    event["pcm"],
                    event.get("response_id"),
                    event.get("item_id"),
                    event.get("content_index", 0),
                )
                if event["pcm"]:
                    self.audio_chunks += 1
                    self.last_audio_response = event.get("response_id")
            elif kind == "tool":
                if event.get("response_id") in self.interrupted_responses:
                    continue
                await self.tool(event)
            elif kind == "tool_cancelled":
                self.manager.event(self.id, "model.tool_cancelled", **event)
                if self.hangup_job and self.hangup_call_id in event["call_ids"]:
                    self.hangup_job.cancel()
                    self.hangup_job = None
            elif kind == "response_started":
                self.responses.add(event["response_id"])
                self.response_events.setdefault(event["response_id"], asyncio.Event())
            elif kind == "turn" and event.get("event") in (
                "input_audio_buffer.speech_started",
                "interrupted",
            ):
                self.interrupted_responses.update(self.responses)
                self.continue_response = False
                self.last_audio_response = None
                if self.result_audio_floor is not None:
                    self.result_audio_floor = self.audio_chunks
                if self.hangup_job and not self.hangup_requested:
                    self.hangup_job.cancel()
                    self.hangup_job = None
                    self.hangup_call_id = None
                if self.bridge:
                    responses, truncations = await self.bridge.interrupt()
                    self.interrupted_responses.update(responses)
                    for item_id, content_index, audio_end_ms in truncations:
                        await self.provider.truncate_audio(item_id, content_index, audio_end_ms)
                    self.manager.event(self.id, "task.audio_interrupted", items=len(truncations))
                self.manager.event(self.id, "model.turn", **event)
            elif kind == "response_done":
                response_id = event["response_id"]
                self.responses.discard(response_id)
                if self.bridge and response_id not in self.interrupted_responses:
                    await self.bridge.finish_response(response_id)
                self.response_events.setdefault(response_id, asyncio.Event()).set()
                self.manager.event(self.id, "model.response", **event)
                if event.get("status") == "failed":
                    raise ProviderError("model response failed")
                if self.continue_response and not self.responses and not self.hangup_job:
                    self.continue_response = False
                    await self.provider.start_response()
            elif kind == "error":
                self.manager.event(self.id, "model.error", **event)
                if event.get("code") != "conversation_already_has_active_response":
                    raise ProviderError("model returned an error event")
            else:
                if kind == "transcript" and event.get("response_id") in self.interrupted_responses:
                    event = {**event, "interrupted": True}
                self.manager.event(self.id, "model." + kind, **event)
        if not self.done.is_set():
            raise ProviderError("model session ended")

    async def tool(self, event):
        call_id, name, raw = event.get("call_id"), event.get("name"), event.get("arguments")
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
            raise ProviderError("tool call has invalid ID")
        if not self.store.claim_tool(self.id, call_id, name, raw):
            return
        self.manager.event(self.id, "tool.call", call_id=call_id, name=name, arguments=raw)
        result = {"ok": False}
        try:
            if not self.bridge or self.done.is_set():
                raise ValueError("phone conversation is not active")
            schema = next((t["parameters"] for t in TOOLS if t["name"] == name), None)
            if schema is None:
                raise ValueError("unknown tool")
            args = json.loads(raw)
            Draft202012Validator(schema).validate(args)
            if name == "send_dtmf":
                await self.backend.slc(self.device).dtmf(args["digits"])
            elif name == "finish_task":
                Draft202012Validator(self.task["input"]["result_schema"]).validate(args["result"])
                if self.store.task(self.id)["model_result"] is not None:
                    raise ValueError("task result already submitted")
                self.store.update_task(self.id, model_result=args)
                self.result_audio_floor = self.audio_chunks
            elif name == "hangup":
                if not event.get("response_id"):
                    raise ValueError("hangup requires a provider response ID")
                has_closing = (
                    self.audio_chunks > self.result_audio_floor
                    if self.result_audio_floor is not None
                    else event["response_id"] == self.last_audio_response
                )
                if not has_closing:
                    self.manager.event(self.id, "task.hangup_deferred", reason="no_spoken_closing")
                    raise ValueError(
                        "Speak a brief closing statement aloud before calling hangup again. "
                        "The hangup reason is not spoken audio. Say only a natural thank-you "
                        "and goodbye in the selected language; do not explain this error or "
                        "any internal procedure. Then retry hangup silently."
                    )
                if self.hangup_job is None:
                    self.hangup_call_id = call_id
                    self.hangup_job = self.spawn(
                        self.normal_hangup(event.get("response_id")), "hangup_failed"
                    )
            result = {"ok": True}
        except (ValueError, TypeError, ValidationError, HFPError) as exc:
            result = {"ok": False, "error": str(exc)[:1000]}
        self.store.finish_tool(self.id, call_id, result)
        self.manager.event(self.id, "tool.result", call_id=call_id, result=result)
        await self.provider.tool_result(call_id, result)
        if name != "hangup" or not result["ok"]:
            self.continue_response = True
            if not self.responses:
                self.continue_response = False
                await self.provider.start_response()

    async def normal_hangup(self, response_id):
        if response_id:
            await self.response_events.setdefault(response_id, asyncio.Event()).wait()
        await self.bridge.drained()
        await self.hangup_phone()
        self.done.set()
