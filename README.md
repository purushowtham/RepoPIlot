# RepoPilot — live local app

RepoPilot runs real AI planning, coding, and review calls against a GitHub repository, validates the proposed patch, executes pytest in a restricted Docker container, and waits for human approval before creating a draft pull request.

**Live mode is the default.** Missing credentials do not silently fall back to a demo: the app opens Live setup and blocks new runs until the model, GitHub credential, and Docker sandbox are verified.

## Open the app

The running workspace is at http://127.0.0.1:5173.

1. Open **Live setup**. If you are using the previous temporary demo account, save a permanent email/password there; existing history stays with the account.
2. Enter your model ID, model API key, and repository-scoped GitHub token. Keys are password fields and are not returned after saving. They are encrypted locally using Fernet; the local encryption key and vault have mode 0600.
3. Select **Save connections securely**, then **Verify connections**. Verification makes a small real model request and checks GitHub authentication; provider charges may apply. The Docker readiness check confirms the runtime and sandbox image are available.
4. Connect your real Python repository, submit an issue, and launch the workflow.
5. Inspect the plan, patch, actual test evidence, and review. Approve only if you want a new branch and draft PR. Nothing is merged automatically.

The earliest permanent local account owns server connection settings. Other accounts cannot change credentials or consume the live server integrations. A temporary demo account must become permanent before storing real credentials.

## Start or restart from the project folder

Python 3.11+ and Node 22+ are required.

```sh
python3 start.py
```

This installs the pinned dependencies if needed, starts API/worker/dashboard, reads `.env` if present, and stops all three when you press Ctrl+C. Use `--port` or `--api-port` if ports are occupied. The running copy on this computer preserves its workspace data in the local `work/repopilot-live-data` directory; the included `.env` points the launcher there. The downloadable ZIP has no local vault, signing key, account database, or `.env`.

The default provider endpoint is `https://api.openai.com/v1`. For another compatible JSON chat provider, configure `LLM_BASE_URL` in `.env` before startup. Model ID and credentials can be changed in Live setup without restarting either service, but connections cannot be rotated while workflows or approvals are pending. Changes invalidate prior verification.

Alternatively, export settings and start each service manually:

```sh
# Terminal 1, from project root
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
uvicorn app.main:app --port 8000
```

```sh
# Terminal 2, from project root
. .venv/bin/activate
cd backend
python -m app.worker
```

```sh
# Terminal 3, from project root
cd frontend
npm ci
npm run dev
```

## Docker on this computer

Colima and the Docker CLI have been installed. A dedicated profile named `repopilot` runs with no host-directory mounts, two CPUs, 2 GiB RAM, and sparse 6 GiB disks. The app automatically uses its socket when available, without changing your default Docker context.

```sh
colima start repopilot --mount none --ssh-config=false --activate=false
```

The `repopilot-sandbox:local` image has been built and tested. On another machine, start Docker and build it:

```sh
docker build -t repopilot-sandbox:local sandbox
```

For this dedicated profile, use `docker --context colima-repopilot ...`. BuildKit or Docker Compose may require their separate CLI plugins; the local app itself does not depend on either plugin. The image was built here with the supported legacy-builder fallback. Disk space is limited on this computer; repository snapshots and execution output remain bounded.

The test runner prepares source in a staging container that **never starts**, commits a temporary local image, and executes code only in a separate non-root, read-only, network-disabled container with memory/CPU/PID/tmpfs/output/time limits. It inspects the isolation settings before startup, records the actual container exit code, and deletes both containers and the temporary source image afterward.

## GitHub token

Use a fine-grained personal token limited to dedicated test repositories, with Contents read/write, Pull requests read/write, and Metadata read. Verification authenticates the token; repository access is checked separately at connection time. GitHub App/OAuth installation flows are not implemented.

## Checks

```sh
PYTHONPATH=backend pytest backend/tests -q
ruff check backend
cd frontend
npm ci
npm run build
```

Actual Docker regression check:

```sh
PYTHONPATH=backend python evals/sandbox_smoke.py
```

The check runs regression tests against the original bug and expects exit code 1, then runs the fixed patch and expects exit code 0. It verifies recorded sandbox isolation metadata. This is a curated fixture, not a live-model coding success benchmark.

Local results: **45 backend checks passed**, TypeScript/Vite production build passed, Ruff passed, and the real Docker test runner passed all three patched regression tests. See [verification](docs/verification.md). No model request or GitHub publication is claimed without actual credentials and recorded evidence.

## Workflow and limits

Validate → planner → pinned Python source snapshot → coder → patch validation → isolated tests → reviewer → human approval → draft PR → final report. Test failures and blocking reviews share a budget of two repairs. The planner, coder, and reviewer use the configured model. Repository retrieval and execution controls are deterministic backend code.

Supported scope: 1–60 visible Python files, 20 KB per file, 500 KB total source, 10 changed files, 100 KB patch, standard library plus preinstalled pytest. No binary changes, deletes, renames, symlinks, hidden-file edits, generated shell commands, or automatic dependency installation. Additional dependencies require an administrator-built sandbox image.

Demo mode is retained only as an explicit option: set `REPOPILOT_MODE=demo` before startup. Old demo history remains labeled as demo when opened in live mode. Demo approvals never create GitHub changes.

This is a private local MVP with one worker and one operator credential set. PostgreSQL/Compose configuration remains included but has not been locally verified. There is no automatic merging, distributed worker leasing, monetary cost ceiling, or hardened multi-tenant sandbox service.

See [architecture](docs/architecture.md), [API](docs/api.md), and [threat model](docs/threat-model.md). MIT licensed.
