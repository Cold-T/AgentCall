import json

import httpx
import pytest

from agentcall.providers.base import ProviderError
from agentcall.providers.live_backend import LiveBackend


async def test_reasoning_replays_calls_and_ledger_without_parallel_execution():
    requests = []
    executed = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
                        {
                            "type": "function_call",
                            "name": "record",
                            "call_id": "call_1",
                            "arguments": '{"answer": "yes"}',
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "Recorded"}]},
                ]
            },
        )

    async def execute(item):
        executed.append(item)
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend(
            "secret", "backend prompt", [{"type": "function", "name": "record"}], client=client
        )
        result = await backend.run(
            [{"role": "user", "content": "Record yes"}],
            [{"name": "already_done"}],
            execute,
        )
        await backend.close()
        assert not client.is_closed
    assert result == "Recorded"
    assert len(executed) == 1
    assert requests[0]["model"] == "gpt-6-luna"
    assert requests[0]["instructions"] == "backend prompt"
    assert requests[0]["store"] is False
    assert requests[0]["parallel_tool_calls"] is False
    assert "already_done" in requests[0]["input"][0]["content"]
    assert requests[1]["input"][-3]["encrypted_content"] == "opaque"
    assert requests[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok": true}',
    }


async def test_backend_failure_after_operation_does_not_retry_or_leak_credentials():
    count = 0
    executed = []

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "one",
                            "name": "record",
                            "arguments": "{}",
                        },
                    ]
                },
            )
        return httpx.Response(500, text="sensitive-secret")

    async def execute(item):
        executed.append(item)
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("sensitive-secret", "prompt", [], client=client)
        with pytest.raises(ProviderError) as error:
            await backend.run([], [], execute)
    assert "sensitive-secret" not in str(error.value)
    assert count == 2
    assert len(executed) == 1


async def test_backend_round_limit():
    count = 0

    def handle(request):
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "one",
                        "name": "record",
                        "arguments": "{}",
                    },
                ]
            },
        )

    async def execute(item):
        nonlocal count
        count += 1
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("secret", "prompt", [], client=client)
        with pytest.raises(ProviderError, match="round limit"):
            await backend.run([], [], execute)
    assert count == 8


@pytest.mark.parametrize("audio", [b"", b"a", b"ab" * (24000 * 15 + 1)])
async def test_farewell_rejects_incomplete_or_oversized_audio(audio):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=audio),
        )
    ) as client:
        backend = LiveBackend("secret", "prompt", [], client=client)
        with pytest.raises(ProviderError):
            await backend.render_farewell("Goodbye", "marin", "English")


async def test_farewell_requests_pcm_and_falls_back_to_supported_voice():
    def handle(request):
        assert request.url.path == "/v1/audio/speech"
        body = json.loads(request.content)
        assert body["voice"] == "marin"
        assert body["response_format"] == "pcm"
        assert body["model"] == "gpt-4o-mini-tts"
        return httpx.Response(200, content=b"\x00\x00" * 240)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("secret", "prompt", [], client=client)
        assert len(await backend.render_farewell("Goodbye", "unsupported", "English")) == 480


async def test_successful_finish_stops_reasoning_and_remaining_tools():
    requests, executed = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "name": "finish_task",
                        "call_id": "finish",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "name": "send_dtmf",
                        "call_id": "late",
                        "arguments": '{"digits":"1"}',
                    },
                ]
            },
        )

    async def execute(call):
        executed.append(call["name"])
        return {"ok": True, "state": "scheduled"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "prompt", [], client=client)
        assert await backend.run([], [], execute) == ""
    assert len(requests) == 1
    assert executed == ["finish_task"]


@pytest.mark.parametrize("result", [{"ok": True}, {"ok": False, "state": "unknown"}])
async def test_dtmf_stops_remaining_tools_and_reasoning(result):
    executed = []
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "name": "send_dtmf",
                        "call_id": "key",
                        "arguments": '{"digits":"2"}',
                    },
                    {
                        "type": "function_call",
                        "name": "finish_task",
                        "call_id": "late",
                        "arguments": "{}",
                    },
                ]
            },
        )

    async def execute(call):
        executed.append(call["name"])
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "hours", [], client=client)
        assert await backend.run([], [], execute) == ""
    assert executed == ["send_dtmf"] and len(requests) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("menu", "menu"),
        ("information", "information"),
        ("waiting", "waiting"),
        ("unknown output", "waiting"),
    ],
)
async def test_feedback_has_no_tools_and_defaults_to_waiting(text, expected):
    def handle(request):
        body = json.loads(request.content)
        assert not body.get("tools")
        return httpx.Response(
            200,
            json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "hours", [], client=client)
        assert await backend.review_dtmf([], "menu text", []) == expected


async def test_information_playback_omits_dtmf_and_rejects_unoffered_call():
    executed = []

    def handle(request):
        body = json.loads(request.content)
        assert [t["name"] for t in body["tools"]] == ["finish_task"]
        assert "information playback" in body["input"][1]["content"]
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "name": "send_dtmf",
                        "call_id": "repeat",
                        "arguments": '{"digits":"1"}',
                    }
                ]
            },
        )

    async def execute(call):
        executed.append(call)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend(
            "fake",
            "hours",
            [
                {"type": "function", "name": "send_dtmf"},
                {"type": "function", "name": "finish_task"},
            ],
            client=client,
        )
        assert await backend.run([], [], execute, allow_dtmf=False) == ""
    assert not executed


async def test_complete_menu_is_delivered_without_key_whitelist():
    from agentcall.tasks.models import TOOLS

    menu = "For staff press one. For hours press two. For pharmacy press four."

    def handle(request):
        body = json.loads(request.content)
        assert menu in body["input"][1]["content"]
        dtmf = next(t for t in body["tools"] if t["name"] == "send_dtmf")
        assert "enum" not in dtmf["parameters"]["properties"]["digits"]
        return httpx.Response(200, json={"output": []})

    async def execute(call):
        raise AssertionError("No operation expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "Confirm hours", TOOLS, client=client)
        assert await backend.run([], [], execute, menu_text=menu) == ""
    assert (
        "enum"
        not in next(t for t in TOOLS if t["name"] == "send_dtmf")["parameters"]["properties"][
            "digits"
        ]
    )


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("menu", "menu"),
        ("information", "information"),
        ("dialogue", "dialogue"),
        ("waiting", "waiting"),
        ("press 0", "waiting"),
    ],
)
async def test_luna_menu_assessment_has_context_but_no_tools(answer, expected):
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert not body.get("tools")
        context = json.loads(body["input"])
        assert context["task"] == "hours"
        assert context["current_input"] == "For hours press two."
        assert context["history"] == [{"role": "user", "content": "Earlier menu"}]
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": answer}]}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "hours", [], client=client)
        assert (
            await backend.review_menu(
                "For hours press two.", [{"role": "user", "content": "Earlier menu"}], []
            )
            == expected
        )


async def test_menu_assessment_failure_does_not_enable_dtmf():
    def handle(request):
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        backend = LiveBackend("fake", "hours", [], client=client)
        assert await backend.review_menu("For hours press two.", [], []) == "waiting"
