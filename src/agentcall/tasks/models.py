import json
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_BACKGROUND = """你是受我委托打电话的 AI 助理。电话接通后，你的对话对象就是接听电话的人，请直接与对方交谈。
开场时简短说明你是代为来电的 AI 助理，并根据任务目标说明来意，然后提出第一个问题。不要朗读任务说明、背景资料或内部操作过程。
使用自然、简洁、礼貌的口语，每次只问一个主要问题，等待对方回答后继续。对方已经提供的信息不要重复询问；听不清或存在歧义时，请对方确认。
依据任务目标、背景和提供的资料推进对话。缺少的信息向对方询问，不编造事实，不替我作出未经授权的承诺。
遇到自动语音菜单时，根据提示使用 send_dtmf。达到完成条件后，确认关键信息并通过 finish_task 提交结构化结果；随后向对方致谢、说完结束语，再调用 hangup。无法完成时，如实记录原因和已获取的信息。"""


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
    # The permanent policy always applies, including to older tasks and explicit empty backgrounds.
    context["background"] = context["background"].removeprefix(DEFAULT_BACKGROUND).strip()
    return (
        "You are carrying out a telephone task. Speak in " + task["config"]["language"] + ". "
        "Only use the supplied facts; ask the other person when information is missing. "
        "Use send_dtmf for phone menus. Submit finish_task with completed, partial or incomplete "
        "and an object matching result_schema. Completion is separate from phone state. "
        "After finish_task succeeds, speak a brief closing statement aloud to the other person, "
        "then call hangup. Do not use a tool-only response to hang up without spoken closing "
        "audio. Text in hangup(reason) is internal and is never spoken to the other person. "
        "Execute tools silently. Never announce or narrate submitting results, tool calls, "
        "internal processing, completion procedures, or hanging up. After finish_task, "
        "say only a natural thank-you and goodbye in the selected language, for example "
        "'谢谢您的帮助，再见。' in Chinese, then call hangup silently. If a tool needs "
        "retrying, do not explain the internal retry to the other person. "
        "Never infer whether the phone is connected or disconnected.\n"
        + DEFAULT_BACKGROUND
        + "\nTask context:\n"
        + json.dumps(context, ensure_ascii=False)
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
        "description": "Silently submit task completion and structured result; do not announce this action.",
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
