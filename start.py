"""Install the pinned dependencies and start a local RepoPilot workspace."""

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Start RepoPilot locally")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Install Python 3.11 or newer first.")
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("Install Node.js 22 or newer (including npm) first.")
    for port in (args.api_port, args.port):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(
                    f"Port {port} is in use. Stop that server or choose --api-port/--port."
                )
    env = os.environ.copy()
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip().replace("_", "").isalnum():
                env.setdefault(key.strip(), value.strip().strip("\"'"))
    env.setdefault("REPOPILOT_MODE", "live")
    env.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    env.setdefault("LANGCHAIN_TRACING_V2", "false")
    env["API_URL"] = f"http://127.0.0.1:{args.api_port}"
    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("Preparing Python environment…", flush=True)
        venv.create(environment, with_pip=True)
    requirements = ROOT / "backend/requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = environment / ".repopilot-lock"
    if not marker.exists() or marker.read_text() != digest:
        print("Installing the pinned backend dependencies…", flush=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(requirements)], check=True
        )
        marker.write_text(digest)
    if not (ROOT / "frontend/node_modules").exists():
        print("Installing dashboard dependencies…", flush=True)
        subprocess.run([npm, "ci"], cwd=ROOT / "frontend", check=True)
    children = []
    try:
        api = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
            ],
            cwd=ROOT / "backend",
            env=env,
        )
        children.append(api)
        ready = False
        for _ in range(60):
            if api.poll() is not None:
                raise RuntimeError("API exited; inspect the startup message above.")
            try:
                urllib.request.urlopen(
                    f"{env['API_URL']}/api/v1/health", timeout=1
                ).close()
                ready = True
                break
            except OSError:
                time.sleep(0.5)
        if not ready:
            raise RuntimeError("API did not become ready within 30 seconds.")
        children.append(
            subprocess.Popen(
                [str(python), "-m", "app.worker"], cwd=ROOT / "backend", env=env
            )
        )
        children.append(
            subprocess.Popen(
                [npm, "run", "dev", "--", "--port", str(args.port), "--strictPort"],
                cwd=ROOT / "frontend",
                env=env,
            )
        )
        print(
            f"\nRepoPilot: http://127.0.0.1:{args.port}\nPress Ctrl+C to stop all three services.\n",
            flush=True,
        )
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise RuntimeError("A service exited; inspect its message above.")
    except KeyboardInterrupt:
        print("\nStopping RepoPilot…")
    finally:
        for child in reversed(children):
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
