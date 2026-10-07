# AgentCall Agent Guide

This repository provides the `agentcall-http` skill for connecting phones, creating AI call tasks, retrieving results, and downloading records through the HTTP API. Read [skills/agentcall-http/SKILL.md](skills/agentcall-http/SKILL.md) before using it; that file defines the operational workflow.

## Install the skill

When the user requests installation, install the entire `skills/agentcall-http/` directory under the name `agentcall-http`. Skill installation does not require deploying the AgentCall service, connecting a phone, or placing a call.

### Codex

Prefer the available `skill-installer` and follow its instructions:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo Cold-T/AgentCall \
  --path skills/agentcall-http \
  --ref v1.0.0
```

The default destination is `$CODEX_HOME/skills/agentcall-http`, or `~/.codex/skills/agentcall-http` when `CODEX_HOME` is unset. Replace `--ref` with the version requested by the user; use `main` for the latest repository content. After installation, tell the user the skill will be available on their next turn.

If `skill-installer` is unavailable, install from the local repository root. This command refuses to overwrite an existing directory:

```bash
python3 - <<'PY'
import os
import shutil
from pathlib import Path

source = Path("skills/agentcall-http")
assert (source / "SKILL.md").is_file(), "Run from the AgentCall repository root"
skill_root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "skills"
skill_root.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, skill_root / "agentcall-http")
PY
```

If the skill is already installed, inspect and compare it before updating. Do not automatically delete the installation or overwrite user modifications. For other agents, copy the same complete directory into their supported skill location and follow their loading instructions. If the runtime does not support skills, read `SKILL.md` directly as the operating guide.

## Configure and use

- Obtain `AGENTCALL_URL` and `AGENTCALL_TOKEN` from user configuration, the environment, or an authorized secret store. Do not guess PINs or display credentials.
- Local APIs commonly use `http://127.0.0.1:8765`; public gateways commonly use HTTPS with an `/api` prefix. The maintainer's instance is `https://agentcall.coldt.uk/api`; use the user's own address for a self-hosted service.
- Send the token in `Authorization: Bearer`. Preserve leading zeroes in fixed PIN mode. Keep credentials out of skill files, task JSON, URLs, logs, and the repository. Model API keys remain service-side.
- Check service and phone readiness as described in the skill. Installation, discovery, and connection do not authorize dialing. Use the target, purpose, and information already authorized by the user; do not automatically redial.
- Use the HTTP API for phone operations. Do not bypass service state with direct Bluetooth or system commands. Installation checks must not place real calls or trigger paid model requests.

## Maintenance and licensing

Update the skill's source directory in this repository, rather than only an agent's installed copy. Keep fields and behavior aligned with the [API reference](docs/api.md), [task guide](docs/tasks.md), and actual endpoints. Keep instructions concise and link to detailed documentation.

For service installation, see [README](README.md) and the [installation guide](docs/install.md). AgentCall's own content is licensed under [MIT](LICENSE); retain applicable copyright notices and license text when copying or distributing it. Upstream attribution and separate license information are documented in [upstream notes](docs/upstream.md).
