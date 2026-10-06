import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from websockets.exceptions import WebSocketException

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
    result = {"Authorization": f"Bearer {token}"} if token else {}
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if bool(client_id) != bool(client_secret):
        emit_error("CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be configured together")
    if client_id:
        result.update({"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret})
    return result


def emit_error(detail, status=None):
    if settings["as_json"]:
        typer.echo(json.dumps({"error": detail, "status": status}, ensure_ascii=False), err=True)
    else:
        Console(stderr=True).print(f"Error: {detail}", markup=False, style="red")
    raise typer.Exit(1)


def http_error(exc):
    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except (ValueError, AttributeError):
            detail = exc.response.text
    else:
        detail = str(exc)
    emit_error(detail, status)


def render(value, path):
    if settings["as_json"]:
        typer.echo(json.dumps(value, ensure_ascii=False))
        return
    if not isinstance(value, list):
        console.print_json(data=value)
        return
    if not value:
        console.print("No records.")
        return
    if "events" in path:
        columns = ["event_id", "time", "kind", "data"]
    elif path.startswith("/tasks") and path.endswith("/tools"):
        columns = ["call_id", "name", "state", "arguments", "result"]
    elif path.startswith("/tasks"):
        columns = ["id", "device", "state", "outcome", "goal"]
    elif path.startswith("/contacts"):
        columns = ["id", "name", "number", "device"]
    elif path.startswith("/devices"):
        columns = ["id", "name", "address", "connected", "hfp_ready"]
        if path.endswith("/saved"):
            columns = ["id", "name", "address", "last_connected", "last_hfp_ready"]
    else:
        columns = ["id", "source", "number", "direction", "state", "duration_seconds", "end_reason"]
    table = Table(*columns, title=path)
    for item in value:
        row = dict(item)
        row.setdefault("goal", row.get("input", {}).get("goal"))
        if "last_observed" in row:
            row.update(row["last_observed"])
            row["last_connected"] = row.get("connected")
            row["last_hfp_ready"] = row.get("hfp_ready")
        if "events" in path:
            row.setdefault("event_id", row.get("id"))
            row.setdefault(
                "data",
                {k: v for k, v in item.items() if k not in ("id", "event_id", "time", "kind")},
            )
        cells = []
        for key in columns:
            cell = row.get(key)
            cells.append(
                Text(
                    "—"
                    if cell is None
                    else json.dumps(cell, ensure_ascii=False)
                    if isinstance(cell, (dict, list))
                    else str(cell)
                )
            )
        table.add_row(*cells)
    console.print(table)


def request(method, path, body=None, params=None, extra_headers=None):
    try:
        response = httpx.request(
            method,
            settings["url"] + path,
            json=body,
            params=params,
            headers={**headers(), **(extra_headers or {})},
            timeout=120,
        )
        response.raise_for_status()
        value = response.json()
    except httpx.HTTPError as exc:
        http_error(exc)
    except ValueError:
        emit_error("Service returned invalid JSON")
    render(value, path)


def filters(**values):
    return {k: v for k, v in values.items() if v is not None}


@app.command()
def health():
    """Show readiness and Bluetooth startup errors."""
    request("GET", "/health")


@app.command()
def devices(
    saved: bool = typer.Option(False, help="Last observed snapshots, available without BlueZ"),
):
    """List current devices or saved snapshots."""
    request("GET", "/devices/saved" if saved else "/devices")


@app.command()
def device(device: str):
    """Show one device, including whether the information is live."""
    request("GET", f"/devices/{device}")


@app.command()
def pairing():
    """List pending numeric pairing confirmations."""
    request("GET", "/pairing")


@app.command()
def scan(action: Annotated[str, typer.Argument(help="start or stop")] = "start"):
    """Start or stop phone discovery."""
    request("POST", f"/discovery/{action}")


@app.command()
def pair(device: str):
    """Pair a phone; use phone watch and phone confirm in another terminal."""
    request("POST", f"/devices/{device}/pair")


@app.command()
def confirm(request_id: str, reject: bool = False):
    """Accept a pending pairing request after checking the phone passkey; --reject refuses it."""
    request("POST", f"/pairing/{request_id}", {"accept": not reject})


@app.command()
def connect(device: str):
    """Connect HFP and enable automatic Bluetooth reconnection without redial."""
    request("POST", f"/devices/{device}/connect")


@app.command()
def disconnect(device: str):
    """Disconnect a phone and disable automatic reconnection."""
    request("POST", f"/devices/{device}/disconnect")


@app.command()
def sync(device: str):
    """Sync PBAP contacts and phone history; the phone may ask for permission."""
    request("POST", f"/devices/{device}/sync")


@app.command()
def contacts(
    query: Annotated[str, typer.Argument(help="Name or phone number")] = "",
    device: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Search contacts by name or number; filter by phone and page results."""
    request("GET", "/contacts", params=filters(q=query, device=device, limit=limit, offset=offset))


@app.command()
def contact(contact_id: str):
    """Show a contact with its original phone vCard and sync time."""
    request("GET", f"/contacts/{contact_id}")


@app.command()
def calls(
    current: bool = False,
    device: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """List calls; --source project|pbap preserves record origin. --current shows live calls."""
    if current and (source or offset or limit != 100):
        raise typer.BadParameter("--current only supports --device")
    request(
        "GET",
        "/calls/current" if current else "/calls",
        params=filters(device=device)
        if current
        else filters(device=device, source=source, limit=limit, offset=offset),
    )


@app.command()
def call(call_id: str, source: str | None = None):
    """Show a project call or PBAP history entry; --source disambiguates IDs."""
    request("GET", f"/calls/{call_id}", params=filters(source=source))


@app.command()
def dial(
    number: Annotated[str | None, typer.Argument()] = None,
    device: str = typer.Option(...),
    contact: str | None = None,
):
    """Manually dial a number or --contact CONTACT_ID using --device PHONE."""
    request("POST", "/calls", {"device": device, "number": number, "contact_id": contact})


@app.command()
def answer(call_id: str):
    """Answer the current incoming call."""
    request("POST", f"/calls/{call_id}/answer")


@app.command()
def hangup(call_id: str):
    """Request phone hangup; actual state changes when the phone confirms it."""
    request("POST", f"/calls/{call_id}/hangup")


@app.command()
def dtmf(call_id: str, digits: str):
    """Send digits, * or # to the active call."""
    request("POST", f"/calls/{call_id}/dtmf", {"digits": digits})


def sse_values(lines):
    data = []
    for line in lines:
        if not line:
            if data:
                yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield json.loads("\n".join(data))


@app.command()
def watch(
    device: str | None = None,
    task: str | None = None,
    call: str | None = None,
    kind: str | None = None,
):
    """Watch live SSE events; --json emits one JSON object per line. Ctrl-C exits cleanly."""
    try:
        with httpx.stream(
            "GET",
            settings["url"] + "/events",
            headers=headers(),
            timeout=None,
            params=filters(device=device, task_id=task, call_id=call, kind=kind),
        ) as response:
            if response.is_error:
                response.read()
            response.raise_for_status()
            for event in sse_values(response.iter_lines()):
                if settings["as_json"]:
                    typer.echo(json.dumps(event, ensure_ascii=False))
                else:
                    console.print(
                        Text(
                            f"{event.get('time', '')} {event['kind']} "
                            + json.dumps(event, ensure_ascii=False)
                        )
                    )
    except httpx.HTTPError as exc:
        http_error(exc)
    except ValueError:
        emit_error("Service returned invalid SSE JSON")
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


@app.command()
def events(
    after: int = 0,
    limit: int = 100,
    device: str | None = None,
    task: str | None = None,
    call: str | None = None,
    kind: str | None = None,
):
    """Read persisted service events after --after EVENT_ID, including missed live events."""
    request(
        "GET",
        "/events/history",
        params=filters(
            after_id=after, limit=limit, device=device, task_id=task, call_id=call, kind=kind
        ),
    )


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
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except (OSError, ValueError, RuntimeError, WebSocketException) as exc:
        emit_error(str(exc))


task_app = typer.Typer(help="Save, start, inspect and cancel AI phone tasks", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.command("create")
def task_create(file: Annotated[Path, typer.Option(exists=True, dir_okay=False)]):
    """Save a task JSON file; start_immediately can enqueue it immediately."""
    try:
        body = json.loads(file.read_text())
    except (OSError, ValueError) as exc:
        emit_error(str(exc))
    request("POST", "/tasks", body)


@task_app.command("start")
def task_start(task_id: str, key: str | None = typer.Option(None)):
    """Enqueue a saved task. Retrying --key never repeats a dial."""
    request(
        "POST", f"/tasks/{task_id}/start", extra_headers={"Idempotency-Key": key} if key else {}
    )


@task_app.command("show")
def task_show(task_id: str):
    """Show task configuration, progress, result, error and physical call state."""
    request("GET", f"/tasks/{task_id}")


@task_app.command("list")
def task_list(
    state: str | None = None,
    device: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Filter saved or executed tasks by phone, state or outcome, with pagination."""
    request(
        "GET",
        "/tasks",
        params=filters(state=state, device=device, outcome=outcome, limit=limit, offset=offset),
    )


@task_app.command("cancel")
def task_cancel(task_id: str):
    """Cancel a saved/queued task, or request cleanup and hangup for an active task."""
    request("POST", f"/tasks/{task_id}/cancel")


@task_app.command("events")
def task_events(task_id: str, after: int = 0, limit: int = 100, kind: str | None = None):
    """Read persisted task events, transcripts and tool results."""
    request(
        "GET", f"/tasks/{task_id}/events", params=filters(after_id=after, limit=limit, kind=kind)
    )


@task_app.command("context")
def task_context(task_id: str, text: str):
    """Add information to the active model conversation."""
    request("POST", f"/tasks/{task_id}/context", {"text": text})


@task_app.command("result")
def task_result(task_id: str):
    """Show structured model result, execution outcome and linked actual phone state."""
    request("GET", f"/tasks/{task_id}/result")


@task_app.command("tools")
def task_tools(task_id: str, limit: int = 100, offset: int = 0):
    """Show tool IDs, arguments, execution state and returned results."""
    request("GET", f"/tasks/{task_id}/tools", params=filters(limit=limit, offset=offset))
