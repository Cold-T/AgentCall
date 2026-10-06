import asyncio
import json
import os
from pathlib import Path

import httpx
import typer
from rich.console import Console

app = typer.Typer(help="Control the AgentCall HTTP service", no_args_is_help=True)
console = Console()
settings = {}


@app.callback()
def options(
    url: str = typer.Option("http://127.0.0.1:8765", envvar="AGENTCALL_URL"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
):
    settings.update(url=url.rstrip("/"), as_json=as_json)


def headers():
    token = os.environ.get("AGENTCALL_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def request(method, path, body=None, params=None):
    try:
        response = httpx.request(
            method, settings["url"] + path, json=body, params=params, headers=headers(), timeout=120
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        if settings["as_json"]:
            typer.echo(json.dumps({"error": detail}, ensure_ascii=False), err=True)
        else:
            Console(stderr=True).print(f"Error: {detail}", markup=False, style="red")
        raise typer.Exit(1) from None
    value = response.json()
    if settings["as_json"]:
        typer.echo(json.dumps(value, ensure_ascii=False))
    else:
        console.print_json(data=value)


@app.command()
def health():
    """Show readiness and Bluetooth startup errors."""
    request("GET", "/health")


@app.command()
def devices():
    request("GET", "/devices")


@app.command()
def scan(action: str = "start"):
    request("POST", f"/discovery/{action}")


@app.command()
def pair(device: str):
    """Pair a phone; use phone watch and phone confirm in another terminal."""
    request("POST", f"/devices/{device}/pair")


@app.command()
def confirm(request_id: str, reject: bool = False):
    request("POST", f"/pairing/{request_id}", {"accept": not reject})


@app.command()
def connect(device: str):
    request("POST", f"/devices/{device}/connect")


@app.command()
def disconnect(device: str):
    request("POST", f"/devices/{device}/disconnect")


@app.command()
def sync(device: str):
    request("POST", f"/devices/{device}/sync")


@app.command()
def contacts(query: str = "", device: str | None = None):
    request("GET", "/contacts", params={"q": query, **({"device": device} if device else {})})


@app.command()
def calls(current: bool = False):
    request("GET", "/calls/current" if current else "/calls")


@app.command()
def dial(number: str | None = None, device: str = typer.Option(...), contact: str | None = None):
    request("POST", "/calls", {"device": device, "number": number, "contact_id": contact})


@app.command()
def answer(call_id: str):
    request("POST", f"/calls/{call_id}/answer")


@app.command()
def hangup(call_id: str):
    request("POST", f"/calls/{call_id}/hangup")


@app.command()
def dtmf(call_id: str, digits: str):
    request("POST", f"/calls/{call_id}/dtmf", {"digits": digits})


@app.command()
def watch():
    """Stream SSE progress, including pairing numeric confirmation."""
    try:
        with httpx.stream(
            "GET", settings["url"] + "/events", headers=headers(), timeout=None
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    typer.echo(line[6:])
    except (httpx.HTTPError, KeyboardInterrupt):
        raise typer.Exit(1) from None


@app.command()
def audio(
    call_id: str,
    input: Path | None = None,
    output: Path | None = None,
    seconds: float = 30,
    live: bool = False,
):
    """Full duplex native PCM: --live uses pacat; files are an optional audio probe."""
    if seconds <= 0 or (not live and not (input or output)):
        raise typer.BadParameter("positive --seconds and --live or --input/--output are required")
    if live and (input or output):
        raise typer.BadParameter("--live cannot be combined with files")
    if input and not input.is_file():
        raise typer.BadParameter(
            "--input must be a regular raw PCM file; use --live for microphone"
        )

    async def run():
        import websockets

        base = settings["url"].replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        async with websockets.connect(
            base + f"/calls/{call_id}/audio",
            additional_headers=headers(),
            max_size=262144,
            max_queue=4,
        ) as ws:
            info = json.loads(await ws.recv())
            typer.echo(json.dumps(info), err=True)
            recorder = player = None
            if live:
                args = [
                    "--raw",
                    "--format=s16le",
                    "--channels=1",
                    f"--rate={info['sample_rate']}",
                    "--latency-msec=10",
                ]
                player = await asyncio.create_subprocess_exec(
                    "pacat", "--playback", *args, stdin=asyncio.subprocess.PIPE
                )
                try:
                    recorder = await asyncio.create_subprocess_exec(
                        "pacat", "--record", *args, stdout=asyncio.subprocess.PIPE
                    )
                except BaseException:
                    player.terminate()
                    await player.wait()
                    raise

            async def rx():
                with output.open("wb") if output else open(os.devnull, "wb") as dest:
                    async for pcm in ws:
                        if isinstance(pcm, bytes):
                            if player:
                                player.stdin.write(pcm)
                                await player.stdin.drain()
                            else:
                                dest.write(pcm)

            async def tx():
                if recorder:
                    remainder = b""
                    while True:
                        data = await recorder.stdout.read(4096)
                        if not data:
                            raise RuntimeError("pacat capture ended")
                        data = remainder + data
                        whole = len(data) - len(data) % 2
                        if whole:
                            await ws.send(data[:whole])
                        remainder = data[whole:]
                elif input:
                    with input.open("rb") as source:
                        size = info["mtu"] if info["codec"] == 1 else 240
                        while data := source.read(size - size % 2):
                            await ws.send(data)
                            # File probe follows sample time; the live bridge adds no timed batches.
                            await asyncio.sleep(len(data) / (info["sample_rate"] * 2))

            tasks = [asyncio.create_task(rx())]
            if recorder or input:
                tasks.append(asyncio.create_task(tx()))
            timer = asyncio.create_task(asyncio.sleep(seconds))
            try:
                while tasks:
                    done, _ = await asyncio.wait(
                        [*tasks, timer], return_when=asyncio.FIRST_COMPLETED
                    )
                    if timer in done:
                        break
                    for task in done:
                        task.result()
                        tasks.remove(task)
            finally:
                for task in [*tasks, timer]:
                    task.cancel()
                await asyncio.gather(*tasks, timer, return_exceptions=True)
                for process in (recorder, player):
                    if process and process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 2)
                        except TimeoutError:
                            process.kill()
                            await process.wait()

    try:
        asyncio.run(run())
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
