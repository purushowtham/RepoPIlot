# Live verification — 9 October 2026

- 45 backend checks passed in 5.76 seconds on Python 3.13.3.
- Ruff checks passed.
- TypeScript/Vite production build passed on Node 22.18.0.
- Encrypted credential storage, permissions, owner restrictions, readiness gating, verification invalidation, safe error messages, secret redaction, temporary-account upgrade, and original workflow tests are covered. External model/GitHub boundaries use test doubles in automated tests.
- Docker CLI 29.9.0 and Colima 0.10.3 installed. Dedicated `repopilot` VM starts with no host-directory mounts. Sparse image preparation was needed because the host disk was nearly full.
- Restricted Python sandbox image built. Real Docker execution of the fixed fixture produced **3 passed**, exit code 0, with simulation false. The smoke check also tests that the original source fails its regression tests.
- Runtime verifies non-root UID 10001, read-only root, no network/host binds, one CPU, 512 MiB memory, and PID limit 64 before executing.

The localhost app is running in live mode and blocks new runs until credentials are entered and verified. No live model-generated patch or actual GitHub branch/PR is claimed yet. PostgreSQL/Compose have not been executed locally. The Docker smoke test validates a curated deterministic patch, not general AI coding ability.
