"""Private local connection vault and live-mode readiness checks."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from . import config

_LOCK = RLock()


def _atomic_write(path: Path, data: bytes):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".connection-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _cipher():
    path = config.DATA / "connections.key"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(path.read_bytes())


def stored():
    path = config.DATA / "connections.enc"
    with _LOCK:
        if not path.exists():
            return {}
        return json.loads(_cipher().decrypt(path.read_bytes()))


def settings():
    values = stored()
    return {
        "llm_api_key": values.get("llm_api_key") or config.LLM_KEY,
        "github_token": values.get("github_token") or config.GITHUB_TOKEN,
        "model": values.get("model") or config.MODEL,
        "base_url": config.LLM_URL,
        "verification": values.get("verification", {}),
    }


def fingerprint(values):
    return hashlib.sha256(
        json.dumps(
            {k: values[k] for k in ("llm_api_key", "github_token", "model", "base_url")}, sort_keys=True
        ).encode()
    ).hexdigest()


def save_connections(body):
    with _LOCK:
        values = stored()
        for key in ("llm_api_key", "github_token"):
            secret = getattr(body, key)
            if secret is not None and secret.get_secret_value().strip():
                values[key] = secret.get_secret_value().strip()
        if body.model is not None:
            values["model"] = body.model.strip()
        values.pop("verification", None)
        _atomic_write(config.DATA / "connections.enc", _cipher().encrypt(json.dumps(values).encode()))


def record_verification(results, expected):
    with _LOCK:
        current = settings()
        if fingerprint(current) != expected:
            raise ValueError("Connections changed during verification. Verify again.")
        values = stored()
        values["verification"] = {"fingerprint": expected, **results}
        _atomic_write(config.DATA / "connections.enc", _cipher().encrypt(json.dumps(values).encode()))


def docker_environment():
    env = os.environ.copy()
    socket = Path.home() / ".colima/repopilot/docker.sock"
    if not env.get("DOCKER_HOST") and not env.get("DOCKER_CONTEXT") and socket.exists():
        env["DOCKER_HOST"] = "unix://" + str(socket)
    return env


def sandbox_status():
    if not shutil.which("docker"):
        return {"ready": False, "message": "Docker is not installed. Install Docker Desktop or Colima."}
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            env=docker_environment(),
        )
        if daemon.returncode:
            return {
                "ready": False,
                "message": "Docker is installed but its runtime is stopped. Start Docker Desktop or Colima.",
            }
        image = subprocess.run(
            ["docker", "image", "inspect", config.SANDBOX_IMAGE],
            capture_output=True,
            timeout=5,
            env=docker_environment(),
        )
        if image.returncode:
            return {
                "ready": False,
                "message": "Sandbox image is missing. Build it with: docker build -t repopilot-sandbox:local sandbox",
            }
        return {
            "ready": True,
            "message": "Docker is running and the isolated Python test image is available.",
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "message": "Docker did not respond. Restart the local container runtime."}


def readiness():
    values = settings()
    verification = values["verification"]
    matches = verification.get("fingerprint") == fingerprint(values)
    ai = bool(matches and verification.get("ai", {}).get("verified"))
    github = bool(matches and verification.get("github", {}).get("verified"))
    sandbox = sandbox_status()
    return {
        "mode": config.MODE,
        "model": values["model"],
        "base_url": values["base_url"],
        "ai": {
            "configured": bool(values["llm_api_key"] and values["model"]),
            "verified": ai,
            "message": verification.get("ai", {}).get(
                "message", "Save your model ID and API key, then verify the connection."
            ),
        },
        "github": {
            "configured": bool(values["github_token"]),
            "verified": github,
            "message": verification.get("github", {}).get(
                "message", "Save a repository-scoped GitHub token, then verify it."
            ),
        },
        "sandbox": sandbox,
        "ready": bool(config.MODE == "live" and ai and github and sandbox["ready"]),
        "verified_at": verification.get("verified_at") if matches else None,
    }


def validate_provider_url():
    url = urlparse(config.LLM_URL)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("The configured model endpoint must be an HTTPS URL without embedded credentials")
