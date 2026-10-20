"""Opt-in paid Live integration check with synthetic audio; never operates a phone.

Run with OPENAI_API_KEY configured. Each run is limited to 60 seconds of Live
connection wall time; backend/TTS usage is separate. Output contains synthetic
conversation and safe error codes only. No credentials are saved.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from agentcall.providers.live import OpenAILive
from agentcall.providers.live_backend import LiveBackend
from agentcall.tasks.models import (
    TOOLS,
    ProviderConfig,
    live_backend_instructions,
    live_instructions,
)


async def main():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit("Configure OPENAI_API_KEY before explicitly running this paid check.")
    task = {
        "config": {"language": "中文"},
        "input": {
            "goal": "向模拟接听方询问办公室哪天几点开门，复述确认后保存结果。",
            "background": "这是获授权的合成语音测试，没有真实电话或外部业务。",
            "information": {},
            "completion_criteria": "询问哪天几点开门，听取纠正，并复述确认最终时间。",
            "result_schema": {"type": "object"},
        },
    }
    synth = LiveBackend(key, "", [])
    phrases = [
        "你好，我们星期五上午九点开门。",
        "嗯。等一下，刚才说错了，是星期四上午十点，请按这个时间记录。",
        "对，是星期四上午十点。谢谢，再见。",
    ]
    clips = []
    try:
        cache = Path("/tmp/agentcall-live-synthetic-input")
        cache.mkdir(exist_ok=True)
        for index, phrase in enumerate(phrases):
            clip = cache / f"caller-{index}.pcm"
            if not clip.exists():
                clip.write_bytes(await synth.render_farewell(phrase, "cedar", "中文"))
            clips.append(clip.read_bytes())
    finally:
        await synth.close()
    provider = OpenAILive(ProviderConfig(model="gpt-live-1"), key)
    provider.conversation_instructions = live_instructions(task)
    report = {
        "model": "gpt-live-1",
        "phone_operations": 0,
        "events": [],
        "audio_bytes": 0,
        "tools": [],
        "errors": [],
    }
    output = bytearray()
    began = time.monotonic()

    async def read_events():
        async for event in provider.events():
            kind = event["kind"]
            if kind == "audio":
                report["audio_bytes"] += len(event["pcm"])
                output.extend(event["pcm"])
            elif kind == "tool":
                result = {"ok": True, "simulation": True}
                try:
                    definition = next(t for t in provider.tools if t["name"] == event["name"])
                    args = json.loads(event["arguments"])
                    Draft202012Validator(definition["parameters"]).validate(args)
                    if event["revision"] != provider.revision:
                        result = {"ok": False, "error": "superseded; not executed"}
                    if event["name"] == "hangup":
                        result["state"] = "simulated_only"
                    report["tools"].append(
                        {"name": event["name"], "arguments": args, "result": result}
                    )
                except Exception:
                    result = {"ok": False, "error": "invalid simulated tool"}
                await provider.tool_result(event["call_id"], result)
            else:
                report["events"].append({"seconds": round(time.monotonic() - began, 2), **event})
                print(json.dumps(report["events"][-1], ensure_ascii=False), flush=True)

    async def feed():
        # Continuous silence plus caller clips; paced at 20ms even before startup.
        pcm_stream = bytes(24000 * 2 * 4)
        for clip in clips:
            pcm_stream += clip + bytes(24000 * 2 * 5)
        for offset in range(0, len(pcm_stream), 960):
            await provider.send_audio(pcm_stream[offset : offset + 960])
            await asyncio.sleep(0.02)
        while True:
            await provider.send_audio(bytes(960))
            await asyncio.sleep(0.02)

    reader = feeder = None
    try:
        await provider.open(live_backend_instructions(task), TOOLS)
        async with asyncio.timeout(60):
            feeder = asyncio.create_task(feed())
            await provider.start_response()
            report["startup_seconds"] = round(time.monotonic() - began, 3)
            reader = asyncio.create_task(read_events())
            await asyncio.gather(reader, feeder)
    except TimeoutError:
        report["stop_reason"] = "planned_60_second_limit"
    except Exception as exc:
        report["errors"].append({"type": type(exc).__name__})
    finally:
        for job in (reader, feeder):
            if job:
                job.cancel()
        await asyncio.gather(*(job for job in (reader, feeder) if job), return_exceptions=True)
        try:
            await provider.close()
        except Exception as exc:
            report["errors"].append({"close_error": type(exc).__name__})
        report["connection_wall_seconds"] = round(time.monotonic() - began, 3)
        report["final_usage"] = provider.final_usage
        report["usage_confirmed"] = provider.closed.is_set()
        root = Path(os.environ.get("AGENTCALL_LIVE_TEST_OUTPUT", "/tmp/agentcall-live-real-test"))
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (root / "output.pcm").write_bytes(output)
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in ("events", "tools")},
                ensure_ascii=False,
            )
        )
        print("Report:", root / "report.json")


if __name__ == "__main__":
    asyncio.run(main())
