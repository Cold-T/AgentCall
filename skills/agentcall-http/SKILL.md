---
name: agentcall-http
description: Operate AgentCall AI phone-call tasks through its authenticated HTTP API. Use to discover or connect a Bluetooth phone, make an AI-assisted call, or retrieve and manage AgentCall tasks.
---

# AgentCall HTTP

Use the HTTP API for phone operations and tests; do not substitute direct Bluetooth or system commands.

## Authentication

Use the configured base URL and credential from the environment or an approved secret store. Public gateways use HTTPS with `/api`; local installations commonly use `http://127.0.0.1:8765`. Include the prefix exactly once.

The configured four-digit PIN is the API token; no login or token exchange is required. Send `Authorization: Bearer <PIN>` or Basic authentication with username `pin` and the PIN as password. Prefer Bearer for automation. Basic mutations require `X-AgentCall-CSRF: 1` and any supplied Origin must match the service; Basic audio WebSockets require a same-origin Origin. Treat the PIN as a string to preserve leading zeroes. Keep credentials out of task bodies, URLs, logs and messages; model API keys remain service-side.

Use authenticated `GET /openapi.json` or `/docs` for fields not covered here. `401` means authentication failed; `429` requires respecting `Retry-After`; `409` means a state conflict; `422` means invalid input; `503` means Bluetooth/contact-sync availability; `504` means timeout. Do not blindly repeat a mutation after a timeout.

## Discover and connect

- `GET /health` checks service readiness. `GET /devices` lists known devices; it does **not** scan. `/devices/saved` is historical, not live discovery. Use identifiers returned by the API.
- To search, enable Bluetooth and open the phone's pairing screen, then `POST /discovery/start`. Discovery is asynchronous: `accepted: true` confirms scan startup only. Wait briefly and check `/devices` as needed for up to 30 seconds. Stop with `POST /discovery/stop` when finished, including after errors; this also stops the webpage's shared scan. An immediate empty list is not a completed search. Do not require the user to start scanning on the webpage.
- Pair an unpaired target with `POST /devices/{device}/pair`. `GET /pairing` exposes pending `requests` with `id`, `device` and, when available, `passkey`. Only after the user verifies the phone's matching code, accept using `POST /pairing/{id}` with `{"accept":true}`; never auto-accept.
- For phone-initiated pairing, use `POST /discoverability/start` instead of scanning. The phone can select the host's Bluetooth name during its 180-second window. Handle confirmations as above and finish with `POST /discoverability/stop`.
- Connect with `POST /devices/{device}/connect`; check `/devices/{device}` for `hfp_ready: true` before dialing. `paired` or `connected` alone is insufficient. Connecting does not authorize a call.
- Disconnect with `POST /devices/{device}/disconnect`. When explicitly asked to forget a phone, use `/unpair`; it retains contacts and history. Do not hang up to bypass an active-call `409`. To repair stale pairing, the user must also forget the host on the phone before pairing again.

## Create and start a call

Start a real call only when the user has authorized its target, purpose and information to share. Saving a task alone does not dial. A new task after a failed or ended attempt is another call and requires authorization; do not automatically redial.

1. Check service readiness and the selected phone's `hfp_ready`.
2. Resolve the recipient with `GET /contacts?q=...&device=...` if needed. A returned contact's **`id`** goes in the task's **`contact_id`**. Resolve ambiguous names/numbers rather than guessing. `POST /devices/{device}/sync` refreshes phone contacts when needed.
3. `POST /tasks` creates a task (`201`, returned `id`). Supply `device`, `goal`, and exactly one of `number` or `contact_id`. Optional fields:

   | Field | Behavior |
   | --- | --- |
   | `information` | Object containing relevant, truthful facts the caller may use. |
   | `background` | Omit to inherit the permanent call instructions. An explicit value replaces them, including an empty string. Override only when requested; do not copy the default instructions into every task or add internal-process narration. |
   | `completion_criteria` | Omitted or blank uses `goal`, matching the webpage. |
   | `result_schema` | JSON Schema for structured output; defaults to an object. References must be local fragments. |
   | `config` | Optional `provider` (`openai`/`gemini`), `model`, `voice`, `language`, `options`. Omit unspecified overrides to inherit saved settings; inspect `GET /settings` if needed. Transcription is enabled by default. |
   | `max_call_seconds` | Positive duration up to 3600; omission inherits the saved default (initially 300 seconds). |
   | `start_immediately` | Defaults to `false`; use `true` only for an authorized call now. |

4. For a saved task, `POST /tasks/{id}/start` with a stable `Idempotency-Key`. `202` means queued, not connected. If the response is uncertain, retrieve the existing task and repeat only the same start with the same key; do not create a replacement task.

Web-selected duration, model and voice are shared service defaults. API overrides affect only that task; saved tasks retain their resolved configuration.

## Status, results and controls

Read status when needed to answer the user or obtain the requested result. Do not continuously poll by default; respect requests to stop monitoring. Stopping monitoring does not cancel the call.

- `GET /tasks/{id}` gives task state; `GET /tasks/{id}/result` gives `outcome`, `model_result`, `error` and linked `call`. Distinguish execution outcome from the model's `completed`/`partial`/`incomplete` assessment and the phone-observed call state. A queued response, model completion or task `ended` alone does not prove the phone hung up or the recipient heard the AI.
- Verify actual phone state with `GET /calls/{call_id}` or `/calls/current` when relevant. Report unavailable evidence rather than inferring success.
- `GET /tasks/{id}/events?after_id=0&limit=100` returns ordered transcript/tool/status events. Paginate with the last ID as the next exclusive `after_id`; optional `kind` filters events. `/tasks/{id}/tools` gives tool calls and results.
- `GET /tasks` accepts `state`, `device`, `outcome`, `limit` and `offset`. Page as needed; do not assume the first page is complete or newest first.
- `POST /tasks/{id}/context` with `{"text":"..."}` adds relevant facts to an active conversation.
- `POST /tasks/{id}/cancel` requests cancellation: queued tasks do not dial; running cleanup and hangup are asynchronous. Verify task and phone state afterward.

## Summaries and downloads

- `GET /tasks/{id}` includes cached `summary` and `downloads.transcript` / `downloads.recording` availability.
- After the task ends, `POST /tasks/{id}/summary` generates or returns its cached GPT-5.6 Luna summary when requested. Generation uses the service key and incurs model usage. Retry a failed summary with `?retry=true` only intentionally, not in a loop.
- `GET /tasks/{id}/transcript` downloads UTF-8 TXT; `404` means unavailable.
- `GET /tasks/{id}/recording` downloads stereo WAV after task end (left: recipient input; right: AI audio sent to the phone). `409` means still running; `404` means unavailable. Missing historical transcripts/recordings cannot be recovered retroactively.

Use the same authenticated base for downloads. Keep call content private and never embed credentials in download links. Summaries are derived records, not proof of audio delivery or hangup.
