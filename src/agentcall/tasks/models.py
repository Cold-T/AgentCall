import json
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_BACKGROUND = """你是受我委托打电话的 AI 助理，直接与接听方交谈。
开场简短介绍身份和来意，并提出第一个问题。背景和资料用于理解任务，交流使用自然、简洁、礼貌的口语。
每次只问一个主要问题，等待回答后继续；利用对方已提供的信息推进任务，听不清或有歧义时请对方确认。
依据任务目标、背景和已知资料交谈，缺失信息向对方询问；只作出已获授权的承诺。"""


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "gemini"] = "openai"
    model: str | None = Field(default=None, min_length=1, max_length=128)
    voice: str | None = Field(default=None, min_length=1, max_length=64)
    language: str = Field(default="中文", min_length=1, max_length=128)
    options: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_options(self):
        self.model = self.model or (
            "gpt-realtime-2.1" if self.provider == "openai" else "gemini-3.8-live"
        )
        self.voice = self.voice or ("marin" if self.provider == "openai" else "Aoede")
        if self.provider == "gemini":
            allowed = {
                "temperature",
                "topP",
                "topK",
                "maxOutputTokens",
                "thinkingConfig",
                "inputAudioTranscription",
                "outputAudioTranscription",
                "realtimeInputConfig",
                "contextWindowCompression",
            }
            if set(self.options) - allowed:
                raise ValueError(
                    "unsupported Gemini option; credentials and endpoint are server-only"
                )
            realtime = self.options.get("realtimeInputConfig", {})
            if (
                not isinstance(realtime, dict)
                or set(realtime)
                - {"automaticActivityDetection", "activityHandling", "turnCoverage"}
                or not isinstance(realtime.get("automaticActivityDetection", {}), dict)
                or realtime.get("automaticActivityDetection", {}).get("disabled", False)
                is not False
                or realtime.get("activityHandling", "START_OF_ACTIVITY_INTERRUPTS")
                != "START_OF_ACTIVITY_INTERRUPTS"
            ):
                raise ValueError(
                    "activity detection and interruption must be handled by the provider"
                )
            self.options.setdefault("inputAudioTranscription", {})
            self.options.setdefault("outputAudioTranscription", {})
            json.dumps(self.options, allow_nan=False)
            return self
        allowed = {
            "turn_detection",
            "noise_reduction",
            "transcription",
            "max_output_tokens",
            "speed",
            "truncation",
        }
        if set(self.options) - allowed:
            raise ValueError(
                "unsupported provider option; credentials and endpoint are server-only"
            )
        detection = self.options.get("turn_detection", {"type": "semantic_vad"})
        if (
            not isinstance(detection, dict)
            or detection.get("type") not in ("semantic_vad", "server_vad")
            or detection.get("create_response", True) is not True
            or detection.get("interrupt_response", True) is not True
        ):
            raise ValueError("turn detection and interruption must be handled by the provider")
        self.options.setdefault("transcription", {"model": "gpt-4o-mini-transcribe"})
        json.dumps(self.options, allow_nan=False)
        return self


class ProviderOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "gemini"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    voice: str | None = Field(default=None, min_length=1, max_length=64)
    language: str | None = Field(default=None, min_length=1, max_length=128)
    options: dict | None = None


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str
    number: str | None = None
    contact_id: str | None = None
    goal: str = Field(min_length=1, max_length=16000)
    background: str = Field(default=DEFAULT_BACKGROUND, max_length=16000)
    information: dict = Field(default_factory=dict)
    completion_criteria: str = Field(default="", max_length=16000)
    result_schema: dict = Field(default_factory=lambda: {"type": "object"})
    config: ProviderOverride = Field(default_factory=ProviderOverride)
    max_call_seconds: float = Field(default=300, gt=0, le=3600)
    start_immediately: bool = False

    @model_validator(mode="after")
    def validate_target(self):
        if not self.completion_criteria.strip():
            self.completion_criteria = self.goal
        if bool(self.number) == bool(self.contact_id):
            raise ValueError("provide exactly one of number or contact_id")
        try:
            Draft202012Validator.check_schema(self.result_schema)
        except SchemaError as exc:
            raise ValueError("invalid result_schema") from exc

        def local_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ("$ref", "$dynamicRef") and not child.startswith("#"):
                        raise ValueError("result_schema references must be local fragments")
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)

        local_refs(self.result_schema)
        json.dumps(self.information, allow_nan=False)
        return self


def instructions(task):
    context = {
        k: task["input"][k]
        for k in ("goal", "background", "information", "completion_criteria", "result_schema")
    }
    background = context.pop("background")
    return (
        "You are an AI assistant carrying out a telephone task for the caller. Speak in "
        + task["config"]["language"]
        + ". Address the recipient directly, using one main question per turn. "
        "Use supplied facts and recipient answers; ask for missing or unclear information. "
        "Use send_dtmf for phone menus. Do not infer connection or disconnection from conversation.\n"
        "Completion:\n"
        "Before finish_task, finish all task-required spoken exchanges: questions, answers, "
        "repetitions, explanations, checks and confirmations. Actually say each required "
        "item, receive required recipient replies, and complete any interrupted exchange. "
        "Plans and text in tool arguments or result fields do not count as spoken actions. "
        "Submit an object matching result_schema with status completed only when the "
        "completion criteria are met; otherwise report unmet requirements honestly as "
        "partial or incomplete. Task completion is separate from phone state.\n"
        "Closing:\n"
        "Execute all tools silently, including retries. Keep internal organizing, recording, "
        "submitting and hanging up out of spoken dialogue, and make no promise of later "
        "processing or reporting. After finish_task succeeds, say only a brief, natural "
        "thank-you and goodbye in the selected language. Let the spoken goodbye audio "
        "finish before calling hangup silently; hangup(reason) is internal text.\n"
        "Background:\n" + background + "\nTask context:\n" + json.dumps(context, ensure_ascii=False)
    )


TOOLS = [
    {
        "type": "function",
        "name": "send_dtmf",
        "description": "Send digits to the phone menu.",
        "parameters": {
            "type": "object",
            "properties": {"digits": {"type": "string", "pattern": "^[0-9*#]{1,64}$"}},
            "required": ["digits"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "finish_task",
        "description": (
            "Silently submit status and structured result after completing all required "
            "spoken exchanges and receiving required replies. Say required content aloud "
            "and finish interrupted exchanges first; plans and tool arguments are not speech. "
            "Report unmet requirements honestly. After success, only a brief spoken "
            "thank-you and goodbye remain before hangup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "partial", "incomplete"]},
                "result": {"type": "object"},
            },
            "required": ["status", "result"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "hangup",
        "description": "Silently end the call after a natural spoken goodbye; do not announce internal procedures.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 1000}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]
