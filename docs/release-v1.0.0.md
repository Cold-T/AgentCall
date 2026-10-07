# v1.0.0 release review

Review date: 2026-10-06. Five subagents reviewed Bluetooth/audio, task/provider lifecycle, API security, storage/CLI/web clients, and release/licensing. The primary agent completed interrupted fixes and final verification. This is a source and software release review; it does not expand the recorded real-phone acceptance scope.

## Findings and resolution

| Area | Finding | Resolution |
| --- | --- | --- |
| Task lifecycle | Repeated cancellation or service shutdown could interrupt finalization, leaving phone cleanup, audio ownership, or the provider connection unfinished. | Cancellation is idempotent while cancellation/finalization is underway; regression covers user cancellation and timeout followed by another cancel or shutdown. |
| Authentication | Browser-cached Basic credentials bypassed the cookie-only CSRF guard. | Basic mutations now require the CSRF marker and reject foreign origins; Basic audio WebSockets require a same-origin Origin. Explicit Bearer automation remains supported. |
| Audio WebSocket | Disconnect during accept or initial status send could leave audio permanently owned. | Handshake is inside the cleanup boundary; regressions cover both failure points. |
| Web history | A slow earlier request could overwrite a newer filter/page selection. | History refresh captures the page and discards stale responses; a Node.js regression reproduces reverse response order. |
| Distribution | Source packages omitted installation files; installing the skill alone omitted its MIT text. | An explicit manifest includes public docs, configuration examples, deployment files, skills, scripts, and tests. The skill carries its own license copy. |
| Multiple phones | SCO establishment is serialized per backend; a ringing phone can delay another phone's audio setup. | Documented in README and installation instructions. Concurrent multi-phone calling is outside the verified v1.0 scope. |

## Verification

- Python 3.13: 227 tests passed, including the new regressions. The only warnings were the existing third-party dbus-next deprecation warnings.
- Ruff lint and formatting, both web JavaScript syntax checks, and Git whitespace checks passed.
- Chromium with a mock phone passed login, tabs, contacts, tasks, provider/voice switching, English 1.0 and Chinese 1.2 speech speeds, saved defaults, paginated transcripts, safe external text rendering, downloads, mobile layout, PIN rotation, and logout. JavaScript errors: 0. No real calls or external model requests were made.
- Locked Python dependencies were checked with pip-audit; no known vulnerabilities were reported by its public advisory service at review time. This does not guarantee the absence of undisclosed vulnerabilities.
- A scan of 380 reachable historical file objects found no common API-token or private-key patterns. Historical paths did not contain private configuration, databases, recordings, or raw audio. This is a scoped check, not a guarantee that every kind of sensitive data can be detected.
- Source/wheel packaging checks cover version metadata, public installation materials, web assets, and both project/upstream MIT notices. Runtime secrets and recordings are excluded.

Remote CI results are available in the repository's Actions history for the release commit.

## Acceptance limits

Android has partial basic-call and PBAP evidence. One iPhone/OpenAI mSBC call demonstrated clear two-way audio, no reported echo, and interruption. Complete phone compatibility, Gemini real API/phone acceptance, continuous-call stability, and the revised task-ending rules' real-model behavior remain unverified. See [acceptance](acceptance.md), [Android](android-acceptance.md), and [iPhone](iphone-acceptance.md) records.
