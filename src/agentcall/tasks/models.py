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

    @property
    def is_live(self) -> bool:
        return self.provider == "openai" and self.model == "gpt-live-1"

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
        if self.is_live:
            # Ignore inherited Realtime controls; Live manages conversational timing.
            json.dumps(self.options, allow_nan=False)
            self.options = {}
            return self
        detection = self.options.get("turn_detection", {"type": "semantic_vad"})
        if (
            not isinstance(detection, dict)
            or detection.get("type") not in ("semantic_vad", "server_vad")
            or detection.get("create_response", True) is not True
            or detection.get("interrupt_response", True) is not True
        ):
            raise ValueError("turn detection and interruption must be handled by the provider")
        self.options.setdefault("transcription", {"model": "gpt-4o-mini-transcribe"})
        language = self.language.strip().casefold().replace("_", "-")
        if language in {"english", "英文", "英语", "en"} or language.startswith("en-"):
            self.options["speed"] = 1.0
        elif language in {
            "中文",
            "普通话",
            "粤语",
            "chinese",
            "mandarin",
            "cantonese",
            "zh",
        } or language.startswith("zh-"):
            self.options["speed"] = 1.2
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


def live_instructions(task):
    """Only conversational behavior belongs in the continuous audio session."""
    return (
        "You are an AI telephone assistant. Speak in " + task["config"]["language"] + ". "
        "Use a brisk, clear conversational pace, without drawn-out syllables or long pauses. "
        "Use this opening template once: 您好，我是一名AI电话助手。给您打电话是想【来电目的】。 "
        "For Chinese, keep the text outside the brackets unchanged. Replace the brackets "
        "with the backend's brief call purpose; never read the brackets or placeholder aloud. "
        "For other conversation languages, use the same meaning in that language. "
        "Wait for the backend's purpose before opening; do not add a separate greeting, "
        "permission question, or task question. After the opening, pause and listen for "
        "the recipient's response before continuing. "
        "Ask one main question at a time, follow backend guidance, and wait for replies.\n"
        "Backchannel policy: Brief listening acknowledgements are welcome without "
        "taking over the recipient's speech.\n"
        "Interruption policy: Yield promptly when the recipient takes the floor, including "
        "during your opening. Repeated hellos or words such as 喂、等一下、先别说 signal "
        "an attempt to speak: stop your sentence and listen, without finishing the introduction "
        "or repeating it afterward. A brief listening acknowledgement such as 嗯 alone "
        "need not stop your answer. Stopping speech does not cancel work.\n"
        "Delegation policy:\n"
        "Backend tools:\n"
        "- Task manager: chooses the next question, checks completion, saves the final result.\n"
        "- Phone controls: plays the final goodbye before disconnecting.\n"
        "Delegate to the backend when:\n"
        "- The recipient answers a task question or confirms information: the backend must "
        "receive the answer to advance or save the task.\n"
        "- They correct information, change a request, or ask to cancel work.\n"
        "- They say goodbye, decline further conversation, or ask to end the call.\n"
        "- A reply needs a tool, decision, or careful reasoning.\n"
        "Do not delegate to the backend when:\n"
        "- You hear an IVR menu or automated recording, including a complete menu.\n"
        "- You hear only a greeting or a listening acknowledgement.\n"
        "- You need to clarify unclear speech or repeat a current verified result.\n"
        "Listen silently to automated recordings; do not assess menu completeness, select keys, "
        "read digits aloud, or interrupt a recording with the opening.\n"
        "Never invent an action result or claim something was saved before backend confirmation. "
        "Never independently end the call; delegate closure to the phone controls."
    )


def live_backend_instructions(task):
    """Task reasoning and tool authority stay outside the Live voice session."""
    context = {
        key: task["input"][key]
        for key in ("goal", "background", "information", "completion_criteria", "result_schema")
    }
    return (
        "You are the task-reasoning backend for an AI telephone assistant. Live handles "
        "listening and speaking in " + task["config"]["language"] + ". "
        "Live uses a fixed introduction and then states the purpose of the call. Before the "
        "conversation has opened, return only one short, recipient-facing purpose clause "
        "derived from the task goal, suitable after 给您打电话是想. Do not include an "
        "introduction, internal tool details, or a question in that opening guidance. "
        "Wait for a recipient response before providing the next task question; if they "
        "already supplied task information, address it instead of restarting the opening. "
        "Give concise dialogue guidance and verified facts. Follow the "
        "task goal, background, supplied information and completion criteria below. "
        "Return a concise coherent spoken-result summary of at most 500 UTF-8 bytes; "
        "keep larger details in task results. Transcript fragments are incomplete and may "
        "arrive out of order; clarify uncertain requests before taking action. "
        "Ask for missing or ambiguous information and make only authorized commitments. "
        "If an automated phone menu is heard, prioritize the task over the opening. "
        "Check recorded answers against the completion criteria before choosing another key. "
        "When recorded information satisfies those criteria, call finish_task without "
        "waiting for silence or navigating the repeated menu. Do not ask an automated "
        "recording a spoken question. Listening to recorded answers satisfies the exchange; "
        "do not require a spoken acknowledgement from an automated system. When the recording "
        "explicitly says the business is closed and supplies the requested regular hours, "
        "submit those facts with any unverified exception marked unknown, using partial "
        "if required details remain. Do not re-enter the hours menu to seek that confirmation. "
        "Distinguish stated regular hours from an explicitly "
        "confirmed date-specific exception; absence of an exception announcement is not "
        "confirmation that none exists. If the recording loops without resolving a required "
        "detail and no useful new route remains, report the known facts and unmet requirement "
        "as partial rather than repeating navigation. "
        "After Luna assesses the current menu as complete, use send_dtmf only if navigation is still needed, not spoken digits. An unfinished menu blocks key selection, not submission of an answer already sufficient for the task. If the answer itself is incomplete, wait for more transcript; do not ask Live to judge completeness or delegate. "
        "Send only the current menu selection, then wait for navigation feedback. "
        "Never repeat accepted or uncertain DTMF operations without confirmed new-menu evidence. "
        "Tools run silently; do not announce internal "
        "recording, submission or hangup procedures. Never infer phone connection from speech. "
        "Stopping speech is not task cancellation. On a corrected request, reconcile pending "
        "work and late results with the latest intent before using them. "
        "A superseded finish_task record requires a new result after the revised exchanges; "
        "this does not authorize repeating external actions. Already executed "
        "operations remain executed: disclose relevant effects, never claim rollback, and "
        "never repeat an operation merely because its result arrived late. "
        "While useful information or confirmation can still be obtained, ask Live to "
        "ask the next question and wait; do not call finish_task just because an answer "
        "has not arrived yet. Use partial/incomplete only when the recipient ends the "
        "conversation or the task cannot proceed. "
        "Before finish_task, complete every required spoken exchange, receive required "
        "recipient replies, and resume interrupted exchanges. Instructions to Live and "
        "tool arguments do not prove the recipient heard anything. Submit an object matching "
        "result_schema with status completed only when the criteria are met; otherwise "
        "report unmet requirements honestly as partial or incomplete. "
        "Include a short natural farewell in the selected language in finish_task. "
        "On success the application automatically plays that exact "
        "farewell and waits for playback before hanging up. New recipient speech can revoke "
        "the pending hangup; address it before attempting closure again. Do not call hangup "
        "separately after finish_task, or ask Live "
        "to independently hang up or promise later processing.\nTask context:\n"
        + json.dumps(context, ensure_ascii=False)
    )
