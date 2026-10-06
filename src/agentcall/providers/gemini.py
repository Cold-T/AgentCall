"""Gemini Developer Live API v1beta; API-owned activity detection and turns."""

import asyncio
import base64
import json
import re

import websockets
from websockets.exceptions import WebSocketException

from agentcall.providers.base import ProviderError


class GeminiLive:
    input_rate = 16000
    output_rate = 24000

    def __init__(
        self,
        config,
        api_key,
        *,
        endpoint="wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
        timeout=20,
    ):
        self.config, self.api_key = config, api_key
        self.endpoint, self.timeout = endpoint, timeout
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.closing = False
        self.started = False
        self.turn = None
        self.turn_number = 0
        self.calls = {}
        self.cancelled_calls = set()

    async def send(self, event):
        if self.ws is None:
            raise ProviderError("model connection is not open")
        try:
            async with self.send_lock:
                await self.ws.send(json.dumps(event, ensure_ascii=False, allow_nan=False))
        except (WebSocketException, OSError) as exc:
            raise ProviderError("model connection disconnected while sending") from exc

    async def receive(self):
        try:
            event = json.loads(await self.ws.recv())
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            return event
        except (WebSocketException, OSError, ValueError) as exc:
            raise ProviderError("Gemini connection ended or sent invalid JSON") from exc

    def session(self, context, tools):
        options = self.config.options
        generation = {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.config.voice}}
            },
        }
        for key in ("temperature", "topP", "topK", "maxOutputTokens", "thinkingConfig"):
            if key in options:
                generation[key] = options[key]
        realtime = {
            "automaticActivityDetection": {"disabled": False},
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
        }
        realtime.update(options.get("realtimeInputConfig", {}))
        setup = {
            "model": "models/" + self.config.model.removeprefix("models/"),
            "generationConfig": generation,
            "systemInstruction": {"parts": [{"text": context}]},
            "realtimeInputConfig": realtime,
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "parametersJsonSchema": t["parameters"],
                            "behavior": "BLOCKING",
                        }
                        for t in tools
                    ]
                }
            ],
        }
        if not tools:
            setup.pop("tools")
        for key in (
            "inputAudioTranscription",
            "outputAudioTranscription",
            "contextWindowCompression",
        ):
            if key in options:
                setup[key] = options[key]
        return setup

    async def open(self, instructions, tools):
        if not self.api_key:
            raise ProviderError("Gemini API key environment variable is not configured")
        try:
            # Same header authentication as google-genai; never put the key in a logged URL.
            self.ws = await websockets.connect(
                self.endpoint,
                additional_headers={"x-goog-api-key": self.api_key},
                open_timeout=self.timeout,
                close_timeout=2,
                max_size=4 * 1024 * 1024,
                max_queue=8,
            )
            async with asyncio.timeout(self.timeout):
                await self.send({"setup": self.session(instructions, tools)})
                while True:
                    event = await self.receive()
                    if "error" in event:
                        raise ProviderError("Gemini rejected session configuration")
                    if "setupComplete" in event:
                        return
                    if any(key in event for key in ("serverContent", "toolCall", "goAway")):
                        raise ProviderError("Gemini sent conversation before setup completed")
        except (WebSocketException, OSError, TimeoutError) as exc:
            await self.close()
            raise ProviderError("Gemini session connection failed or timed out") from exc
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, pcm):
        if len(pcm) % 2:
            raise ValueError("PCM must contain complete 16-bit samples")
        if pcm:
            await self.send(
                {
                    "realtimeInput": {
                        "audio": {
                            "mimeType": "audio/pcm;rate=16000",
                            "data": base64.b64encode(pcm).decode(),
                        }
                    }
                }
            )

    async def start_response(self):
        # Gemini resumes automatically after toolResponse. Only initiate the first greeting.
        if not self.started:
            self.started = True
            await self.send(
                {"realtimeInput": {"text": "The phone is connected. Begin the task now."}}
            )

    async def update_context(self, text):
        await self.send({"realtimeInput": {"text": "Additional task information: " + text}})

    async def tool_result(self, call_id, result):
        if call_id in self.cancelled_calls:
            return
        if call_id not in self.calls:
            raise ProviderError("unknown Gemini tool call ID")
        await self.send(
            {
                "toolResponse": {
                    "functionResponses": [
                        {
                            "id": call_id,
                            "name": self.calls[call_id],
                            "response": result,
                        }
                    ]
                }
            }
        )

    def start_turn(self):
        if self.turn is None:
            self.turn_number += 1
            self.turn = f"gemini-turn-{self.turn_number}"
            return {"kind": "response_started", "response_id": self.turn}
        return None

    async def events(self):
        while not self.closing:
            event = await self.receive()
            if "error" in event:
                error = event["error"]
                yield {
                    "kind": "error",
                    "code": str(error.get("code", "gemini_error")),
                    "type": error.get("status"),
                }
            if "toolCallCancellation" in event:
                ids = event["toolCallCancellation"].get("ids", [])
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    raise ProviderError("Gemini sent invalid tool cancellation")
                self.cancelled_calls.update(ids)
                yield {"kind": "tool_cancelled", "call_ids": ids}
            content = event.get("serverContent", {})
            functions = event.get("toolCall", {}).get("functionCalls", [])
            if content.get("modelTurn") or content.get("outputTranscription") or functions:
                if started := self.start_turn():
                    yield started
            for part in content.get("modelTurn", {}).get("parts", []):
                if part.get("thought"):
                    continue
                if "inlineData" in part:
                    blob = part["inlineData"]
                    if not re.fullmatch(r"audio/pcm;\s*rate=24000", blob.get("mimeType", "")):
                        raise ProviderError("Gemini sent unsupported output audio format")
                    try:
                        pcm = base64.b64decode(blob["data"], validate=True)
                        if len(pcm) % 2:
                            raise ValueError("odd PCM length")
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ProviderError("Gemini sent invalid PCM audio") from exc
                    yield {"kind": "audio", "pcm": pcm, "response_id": self.turn}
                if "text" in part:
                    yield {"kind": "text", "text": part["text"], "response_id": self.turn}
            for key, role in (("inputTranscription", "user"), ("outputTranscription", "assistant")):
                if key in content:
                    transcription = content[key]
                    yield {
                        "kind": "transcript",
                        "role": role,
                        "text": transcription.get("text", ""),
                        "delta": True,
                        "finished": transcription.get("finished", False),
                        "response_id": self.turn,
                    }
            for call in functions:
                call_id, name = call.get("id"), call.get("name")
                if (
                    not isinstance(call_id, str)
                    or not 1 <= len(call_id) <= 256
                    or not isinstance(name, str)
                ):
                    raise ProviderError("Gemini sent invalid tool ID or name")
                if call_id in self.cancelled_calls:
                    continue
                if call_id in self.calls and self.calls[call_id] != name:
                    raise ProviderError("Gemini reused tool ID with a different name")
                self.calls[call_id] = name
                try:
                    arguments = json.dumps(call.get("args", {}), allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ProviderError("Gemini sent invalid tool arguments") from exc
                yield {
                    "kind": "tool",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "response_id": self.turn,
                }
            if content.get("interrupted"):
                yield {"kind": "turn", "event": "interrupted", "response_id": self.turn}
            if content.get("generationComplete"):
                yield {"kind": "turn", "event": "generation_complete", "response_id": self.turn}
            if content.get("turnComplete"):
                # IN_PROGRESS can mean a multi-step interaction still has pending generation.
                if content.get("interactionStatus") == "IN_PROGRESS":
                    yield {
                        "kind": "turn",
                        "event": "turn_complete_in_progress",
                        "response_id": self.turn,
                    }
                elif self.turn:
                    yield {
                        "kind": "response_done",
                        "response_id": self.turn,
                        "status": "completed",
                        "usage": event.get("usageMetadata"),
                    }
                    self.turn = None
            if "goAway" in event:
                yield {"kind": "session_ending", "time_left": event["goAway"].get("timeLeft")}
            if "usageMetadata" in event:
                yield {"kind": "usage", "usage": event["usageMetadata"]}

    async def close(self):
        self.closing = True
        if self.ws:
            await self.ws.close()
