"""Opt-in Gemini API session/audio check; no phone dialing."""

import asyncio
import os

from agentcall.providers.gemini import GeminiLive
from agentcall.tasks.models import ProviderConfig


async def main():
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise SystemExit("Set GEMINI_API_KEY in the service environment before running this check.")
    provider = GeminiLive(
        ProviderConfig(
            provider="gemini", model=os.environ.get("AGENTCALL_GEMINI_MODEL", "gemini-3.8-live")
        ),
        key,
    )
    try:
        async with asyncio.timeout(30):
            await provider.open("Say a brief hello in Chinese, then stop speaking.", [])
            await provider.send_audio(bytes(1600))  # 50ms PCM input protocol probe
            await provider.start_response()
            audio_bytes = 0
            async for event in provider.events():
                if event["kind"] == "audio":
                    audio_bytes += len(event["pcm"])
                elif event["kind"] == "error":
                    raise RuntimeError("Gemini API returned an error: " + str(event.get("code")))
                elif event["kind"] == "response_done":
                    if not audio_bytes:
                        raise RuntimeError("Gemini API did not produce an audio response")
                    print(
                        f"PASS: PCM input accepted; received {audio_bytes} bytes of 24kHz PCM output"
                    )
                    return
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
