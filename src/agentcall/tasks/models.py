import json
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    background: str = Field(default="", max_length=16000)
    information: dict = Field(default_factory=dict)
    completion_criteria: str = Field(default="", max_length=16000)
    result_schema: dict = Field(default_factory=lambda: {"type": "object"})
    config: ProviderOverride = Field(default_factory=ProviderOverride)
    max_call_seconds: float = Field(default=300, gt=0, le=3600)
    start_immediately: bool = False

    @model_validator(mode="after")
    def validate_target(self):
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
    return (
        "You are carrying out a telephone task. Speak in " + task["config"]["language"] + ". "
        "Only use the supplied facts; ask the other person when information is missing. "
        "Use send_dtmf for phone menus. Submit finish_task with completed, partial or incomplete "
        "and an object matching result_schema. Completion is separate from phone state. "
        "After submitting the result, give a brief closing statement, then call hangup. "
        "Never infer whether the phone is connected or disconnected. Task context:\n"
        + json.dumps(
            {
                k: task["input"][k]
                for k in (
                    "goal",
                    "background",
                    "information",
                    "completion_criteria",
                    "result_schema",
                )
            },
            ensure_ascii=False,
        )
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
        "description": "Submit task completion and structured result.",
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
        "description": "End the call after existing spoken audio is sent.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 1000}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]
