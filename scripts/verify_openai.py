"""Opt-in real API session/audio smoke check; never dials a phone."""

import asyncio
import os

from agentcall.providers.openai import OpenAIRealtime
from agentcall.tasks.models import ProviderConfig


async def main():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit("Set OPENAI_API_KEY in the service environment before running this check.")
    config = ProviderConfig(model=os.environ.get("AGENTCALL_OPENAI_MODEL", "gpt-realtime-2.1"))
    provider = OpenAIRealtime(config, key)
    try:
        async with asyncio.timeout(30):
            await provider.open("Say a brief hello in Chinese, then stop speaking.", [])
            await provider.send_audio(bytes(2400))  # PCM input protocol check, 50ms silence
            await provider.start_response()
            audio_bytes = 0
            async for event in provider.events():
                if event["kind"] == "audio":
                    audio_bytes += len(event["pcm"])
                elif event["kind"] == "error":
                    raise RuntimeError("Realtime API returned an error: " + str(event.get("code")))
                elif event["kind"] == "response_done":
                    if event["status"] != "completed" or not audio_bytes:
                        raise RuntimeError("Realtime API did not complete an audio response")
                    print(
                        f"PASS: PCM input accepted; received {audio_bytes} bytes of 24kHz PCM output"
                    )
                    return
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
