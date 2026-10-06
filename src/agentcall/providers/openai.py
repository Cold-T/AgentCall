"""OpenAI Realtime GA WebSocket protocol; continuous input, server turn detection."""

import asyncio
import base64
import json
from urllib.parse import urlencode

import websockets
from websockets.exceptions import WebSocketException

from agentcall.providers.base import ProviderError


class OpenAIRealtime:
    input_rate = output_rate = 24000

    def __init__(self, config, api_key, *, endpoint="wss://api.openai.com/v1/realtime", timeout=20):
        self.config = config
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.closing = False

    async def send(self, event):
        if self.ws is None:
            raise ProviderError("model connection is not open")
        try:
            async with self.send_lock:
                await self.ws.send(json.dumps(event, ensure_ascii=False))
        except (WebSocketException, OSError) as exc:
            raise ProviderError("model connection disconnected while sending") from exc

    async def receive(self):
        try:
            event = json.loads(await self.ws.recv())
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            return event
        except (WebSocketException, OSError, ValueError) as exc:
            raise ProviderError("model connection ended or sent invalid JSON") from exc

    def session(self, context, tools):
        options = self.config.options
        detection = {"type": "semantic_vad", "create_response": True, "interrupt_response": True}
        detection.update(options.get("turn_detection", {}))
        input_audio = {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": detection}
        for key in ("noise_reduction", "transcription"):
            if key in options:
                input_audio[key] = options[key]
        output_audio = {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.config.voice}
        if "speed" in options:
            output_audio["speed"] = options["speed"]
        session = {
            "type": "realtime",
            "model": self.config.model,
            "instructions": context,
            "output_modalities": ["audio"],
            "audio": {"input": input_audio, "output": output_audio},
            "tools": tools,
            "tool_choice": "auto",
        }
        for key in ("max_output_tokens", "truncation"):
            if key in options:
                session[key] = options[key]
        return session

    async def open(self, instructions, tools):
        if not self.api_key:
            raise ProviderError("OpenAI API key environment variable is not configured")
        try:
            self.ws = await websockets.connect(
                self.endpoint + "?" + urlencode({"model": self.config.model}),
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                open_timeout=self.timeout,
                close_timeout=2,
                max_size=4 * 1024 * 1024,
                max_queue=8,
            )
            async with asyncio.timeout(self.timeout):
                await self.send(
                    {"type": "session.update", "session": self.session(instructions, tools)}
                )
                while True:
                    event = await self.receive()
                    if event.get("type") == "error":
                        raise ProviderError("OpenAI rejected session configuration")
                    if event.get("type") == "session.updated":
                        session = event.get("session", {})
                        for direction in ("input", "output"):
                            fmt = session.get("audio", {}).get(direction, {}).get("format", {})
                            if fmt.get("type") != "audio/pcm" or fmt.get("rate") != 24000:
                                raise ProviderError("OpenAI resolved unsupported audio format")
                        return
        except (WebSocketException, OSError, TimeoutError) as exc:
            await self.close()
            # Do not expose exception strings that can contain credentials or request headers.
            raise ProviderError("OpenAI session connection failed or timed out") from exc
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, pcm):
        if len(pcm) % 2:
            raise ValueError("PCM must contain complete 16-bit samples")
        if pcm:
            await self.send(
                {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
            )

    async def start_response(self):
        await self.send({"type": "response.create"})

    async def truncate_audio(self, item_id, content_index, audio_end_ms):
        await self.send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": content_index,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def update_context(self, text):
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def tool_result(self, call_id, result):
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                },
            }
        )

    async def events(self):
        while not self.closing:
            event = await self.receive()
            kind = event.get("type")
            common = {"response_id": event.get("response_id")}
            if kind == "response.output_audio.delta":
                try:
                    pcm = base64.b64decode(event["delta"], validate=True)
                    if len(pcm) % 2:
                        raise ValueError("odd PCM length")
                except (ValueError, KeyError) as exc:
                    raise ProviderError("OpenAI sent invalid PCM audio") from exc
                yield {
                    "kind": "audio",
                    "pcm": pcm,
                    "item_id": event.get("item_id"),
                    "content_index": event.get("content_index", 0),
                    **common,
                }
            elif kind in ("response.output_item.done", "response.function_call_arguments.done"):
                item = event.get("item", event)
                if (
                    item.get("type") == "function_call"
                    or kind == "response.function_call_arguments.done"
                ):
                    yield {
                        "kind": "tool",
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                        **common,
                    }
            elif kind in (
                "response.output_audio_transcript.done",
                "conversation.item.input_audio_transcription.completed",
            ):
                yield {
                    "kind": "transcript",
                    "role": "assistant" if kind.startswith("response") else "user",
                    "text": event.get("transcript", ""),
                    "item_id": event.get("item_id"),
                    **common,
                }
            elif kind == "response.created":
                yield {"kind": "response_started", "response_id": event["response"]["id"]}
            elif kind == "response.done":
                response = event["response"]
                yield {
                    "kind": "response_done",
                    "response_id": response["id"],
                    "status": response.get("status"),
                    "status_details": response.get("status_details"),
                    "usage": response.get("usage"),
                }
            elif kind in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
                yield {"kind": "turn", "event": kind}
            elif kind == "error":
                # Preserve safe provider error code/type, never arbitrary echo of client secrets.
                error = event.get("error", {})
                yield {"kind": "error", "code": error.get("code"), "type": error.get("type")}

    async def close(self):
        self.closing = True
        if self.ws:
            await self.ws.close()
