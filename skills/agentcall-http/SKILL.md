---
name: agentcall-http
description: Create, start, monitor, and retrieve AgentCall AI phone-call tasks through its authenticated HTTP API. Use when asked to make an AI-assisted phone call with AgentCall or manage an existing AgentCall task over HTTP.
---

# AgentCall HTTP task skill

Use this skill to operate AgentCall's AI task-call feature over HTTP. The service controls a paired phone through Bluetooth and connects a configured OpenAI or Gemini realtime model to the call.

## Authorization and safety

- A task start can place a real phone call. Start one only when the user has clearly asked you to make that call. Creating a task with `start_immediately: false` only saves it; it does not dial.
- Before starting, make sure the user provided or approved the target phone/contact, the purpose, and the information the caller may share. Do not invent facts or contact someone for a materially different purpose.
- Do not retry a task by creating another one just because a request timed out. First retrieve its status; use the same idempotency key when repeating a start request.
- Keep the AgentCall PIN/token and all model credentials secret. Never put credentials in task JSON, URL query parameters, logs, or the conversation. Model API keys belong in the AgentCall service environment.
- A task's outcome, model-reported result, and the phone's actual call state are separate. Report each from the API; do not claim the call ended based only on a model result.

## Connection and authentication

Use the configured service URL and credential from the caller's environment or approved secret store. Do not guess credentials. Local installations commonly use `http://127.0.0.1:8765`; installations behind the public gateway use the `/api` prefix and HTTPS.

```sh
export AGENTCALL_URL='http://127.0.0.1:8765'  # or https://host.example/api
# AGENTCALL_TOKEN must be provided securely by the environment.
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${AGENTCALL_TOKEN}" \
  "${AGENTCALL_URL}/health"
```

The configured 4-digit PIN is the API token itself; no login or token-exchange step is required for API clients. The configured PIN can be sent as a Bearer credential. HTTP Basic authentication with username `pin` and the PIN as password is also supported, for example `curl -u "pin:${AGENTCALL_TOKEN}"`. Preserve leading zeroes by treating PINs as strings. If the service uses a root path such as `/api`, include it in `AGENTCALL_URL` exactly once. Use `/docs` or `/openapi.json` under that base URL when you need the live schema.

Common failures: `401` means authentication failed; `429` means authentication rate limiting and includes `Retry-After`; `422` means request schema/field validation failed; `409` indicates a phone/task/AT-command state conflict; `503` indicates Bluetooth or PBAP availability; `504` indicates a timeout. Do not repeat a mutating request blindly after a timeout.

## Make a task call

1. Check readiness (`GET /health`) and list devices (`GET /devices`). Use a device identifier returned by the service. A device can be a Bluetooth MAC such as `AA:BB:CC:DD:EE:FF` or the supported `dev_AA_BB_CC_DD_EE_FF` form.
2. If needed, find a contact with `GET /contacts?q=...` and use its `contact_id`. Do not assume a contact is current unless the service reports it.
3. Build a task using exactly one of `number` or `contact_id`. The task fields are:
   - `device` (required): target paired phone.
   - `number` or `contact_id` (exactly one): call target.
   - `goal` (required): what the model should accomplish.
   - `background` (optional): only additional facts for this call. Omit it for ordinary requests; the service permanently supplies the full AI-assistant calling policy. An explicit empty or custom background does not remove that policy.
   - `information`: optional truthful facts the caller may use.
   - `completion_criteria`: use the same value as `goal` unless the user requests a more specific success criterion.
   - `result_schema`: JSON Schema for the structured result; references must be local fragments.
   - `config`: optional `provider` (`openai` or `gemini`), `model`, `voice`, `language`, and provider `options`. Credentials and model endpoints are service-side only.
   - `max_call_seconds`: optional, 1–3600; defaults to 300.
   - `start_immediately`: leave `false` unless the user explicitly authorized calling now.
4. Create it with `POST /tasks`. A `201` response includes the task record and its `id`.
5. When authorized, start with `POST /tasks/{id}/start`, sending a stable `Idempotency-Key`. The response is `202`: this means accepted/queued, not that the call connected.
6. Poll `GET /tasks/{id}` or `GET /tasks/{id}/result` until the task is ended. Fetch `GET /tasks/{id}/events` for the ordered transcript/tool/status events. Use `GET /calls/{call_id}` to verify the actual phone call details when a `call_id` is present.

Example request body (replace all example identifiers and text with user-provided values):

```json
{
  "device": "AA:BB:CC:DD:EE:FF",
  "number": "+15551234567",
  "goal": "Ask whether the office is open tomorrow and what its hours are.",
  "information": {},
  "completion_criteria": "Ask whether the office is open tomorrow and what its hours are.",
  "result_schema": {
    "type": "object",
    "properties": {
      "opening_time": {"type": "string"},
      "closing_time": {"type": "string"},
      "notes": {"type": "string"}
    },
    "required": ["opening_time", "closing_time", "notes"],
    "additionalProperties": false
  },
  "config": {"provider": "openai", "language": "English"},
  "max_call_seconds": 300,
  "start_immediately": false
}
```

Example create, start, and result requests:

```sh
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${AGENTCALL_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data @task.json \
  "${AGENTCALL_URL}/tasks"

# Use the returned task ID; only do this after authorization to place the call.
curl --fail-with-body --silent --show-error -X POST \
  -H "Authorization: Bearer ${AGENTCALL_TOKEN}" \
  -H 'Idempotency-Key: agentcall-<unique-stable-key>' \
  "${AGENTCALL_URL}/tasks/<task-id>/start"

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${AGENTCALL_TOKEN}" \
  "${AGENTCALL_URL}/tasks/<task-id>/result"
```

Task states progress through `saved → queued → preparing → dialing → in_call → finalizing → ended`. Start is idempotent: retry the same task with the same key after an uncertain response. A task that has ended cannot be restarted; a new task is a new call and requires authorization. The phone queue serializes tasks per device, and conflicts may return `409`.

The permanent service instructions make the AI introduce itself as a delegated assistant, talk directly to the recipient, ask one main question at a time, avoid reading internal task/context text, avoid invented facts or unauthorized commitments, use `send_dtmf` for phone menus, and submit a truthful structured result with `finish_task`. After completing the task it must confirm key facts, thank the recipient, finish speaking its closing, then call `hangup`. Do not copy these instructions into each task request; supply the call goal and relevant facts only.

## Monitor, add context, cancel

- `GET /tasks/{id}/events?after_id=0&limit=100` returns task events in ascending ID order. Pass the last event ID as the next `after_id` (exclusive cursor) to paginate. `kind` can filter events.
- `GET /tasks/{id}/tools?limit=100&offset=0` returns model tool calls and their results.
- `POST /tasks/{id}/context` with `{"text":"..."}` adds information to an active conversation only. Use it only for relevant, accurate context.
- `POST /tasks/{id}/cancel` requests cancellation. A queued task ends without dialing; a running task is cleaned up asynchronously and hangup is requested. Retrieve the task and call records afterward to verify the phone state.
- `GET /tasks` accepts filters such as `state`, `device`, and `outcome`, plus `limit` and `offset`. Page through results rather than assuming the first response is complete.

For live updates, `GET /events` is Server-Sent Events. It has no replay; on reconnect, query task/current state or use the persistent `/events/history` endpoint. If an SSE client receives `events.overflow`, reconnect and query current state.

## Result interpretation

Read `/tasks/{id}/result` and distinguish:

- `outcome`: why AgentCall execution ended (for example completed, error, canceled, or timeout).
- `model_result.status`: the model's `completed`, `partial`, or `incomplete` assessment and structured result.
- `call.state`: phone-observed call state, which is authoritative for whether the phone is still in a call.
- `error`: service/model/telephony failure details, when present.

Summarize facts grounded in the result and events. Mark missing fields as unavailable instead of inferring them. A `202` start response, successful `finish_task`, or task `ended` state alone does not prove that the phone is idle.

## API reference

Use the service's authenticated `GET /docs` or `GET /openapi.json` for the complete current schema. Project documentation: `docs/api.md`, `docs/tasks.md`, and `docs/gemini.md`.
