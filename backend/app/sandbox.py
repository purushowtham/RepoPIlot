import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import PurePosixPath
from uuid import uuid4
from .config import SANDBOX_IMAGE, SANDBOX_TIMEOUT
from .patches import safe_path
from .security import redact
from .connections import docker_environment

ALLOWED_COMMANDS = {"pytest": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]}


def run_tests(files, command_name="pytest"):
    if command_name not in ALLOWED_COMMANDS:
        raise ValueError("Test command is not allowlisted")
    if not shutil.which("docker"):
        raise RuntimeError(
            "Docker is required for real tests. Install Docker and build repopilot-sandbox:local; no generated code was executed."
        )
    for path in files:
        safe_path(path)
    if len(files) > 70 or sum(len(source.encode()) for source in files.values()) > 500000:
        raise ValueError("Sandbox snapshot exceeds its source budget")
    environment = docker_environment()
    started = time.monotonic()
    name = "repopilot-test-" + uuid4().hex
    staging = name + "-seed"
    image = "repopilot-patch:" + uuid4().hex
    status = "failed"
    exit_code = None
    output = ""
    try:
        # This staging container NEVER starts. Docker cannot copy into a
        # read-only container, so create an immutable local source image first.
        seeded = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                staging,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                SANDBOX_IMAGE,
                "python",
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        if seeded.returncode:
            raise RuntimeError("Sandbox could not be prepared. Check Docker and the prebuilt sandbox image.")
        # Only backend-generated archive headers are trusted. No repository
        # archives are extracted on the host, and no host directories are mounted.
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                parents = sorted(
                    {
                        str(parent)
                        for path in files
                        for parent in PurePosixPath(path).parents
                        if str(parent) != "."
                    },
                    key=lambda p: (p.count("/"), p),
                )
                for path in parents:
                    info = tarfile.TarInfo(path)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.uid = info.gid = 10001
                    tar.addfile(info)
                for path, source in files.items():
                    info = tarfile.TarInfo(path)
                    data = source.encode()
                    info.size = len(data)
                    info.mode = 0o644
                    info.uid = info.gid = 10001
                    tar.addfile(info, io.BytesIO(data))
            archive.seek(0)
            copied = subprocess.run(
                ["docker", "cp", "-", staging + ":/seed"],
                stdin=archive,
                capture_output=True,
                timeout=20,
                env=environment,
            )
            if copied.returncode:
                raise RuntimeError("Could not stage the bounded source snapshot")
        committed = subprocess.run(
            ["docker", "commit", staging, image], capture_output=True, timeout=20, env=environment
        )
        if committed.returncode:
            raise RuntimeError("Could not seal the sandbox source image")
        subprocess.run(["docker", "rm", staging], capture_output=True, timeout=15, env=environment)
        create = [
            "docker",
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--user",
            "10001:10001",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            image,
            *ALLOWED_COMMANDS[command_name],
        ]
        created = subprocess.run(create, capture_output=True, text=True, timeout=20, env=environment)
        if created.returncode:
            raise RuntimeError("The restricted test container could not be created")
        inspect = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, timeout=10, env=environment
        )
        if inspect.returncode:
            raise RuntimeError("Sandbox isolation could not be verified")
        container = json.loads(inspect.stdout)[0]
        host = container["HostConfig"]
        if not (
            host["ReadonlyRootfs"]
            and host["NetworkMode"] == "none"
            and container["Config"]["User"] == "10001:10001"
            and not host.get("Binds")
            and not host.get("Privileged")
            and "ALL" in host["CapDrop"]
            and host["Memory"] == 536870912
            and host["PidsLimit"] == 64
            and host["NanoCpus"] == 1000000000
        ):
            raise RuntimeError("Sandbox isolation checks failed; no generated code was executed")
        evidence = {
            "read_only": True,
            "network": "none",
            "user": "10001:10001",
            "memory_bytes": host["Memory"],
            "pids_limit": host["PidsLimit"],
            "cpus": 1,
            "host_mounts": False,
        }
        with tempfile.TemporaryFile() as logs:
            proc = subprocess.Popen(
                ["docker", "start", "-a", name], stdout=logs, stderr=logs, env=environment
            )
            deadline = time.monotonic() + SANDBOX_TIMEOUT
            while proc.poll() is None:
                if time.monotonic() >= deadline or logs.tell() > 2_000_000:
                    subprocess.run(["docker", "kill", name], capture_output=True, timeout=10, env=environment)
                    proc.wait(timeout=10)
                    status = "timeout" if time.monotonic() >= deadline else "output_limit"
                    break
                time.sleep(0.1)
            else:
                inspection = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=environment,
                )
                state = json.loads(inspection.stdout)[0]["State"]
                if state["Status"] != "exited" or state.get("Error"):
                    raise RuntimeError("The sandbox process could not start; no passing result was recorded")
                exit_code = int(state["ExitCode"])
                status = "passed" if exit_code == 0 else "failed"
            logs.seek(0)
            output = redact(logs.read(64000).decode("utf-8", errors="replace"))
        return {
            "command_name": command_name,
            "command": " ".join(ALLOWED_COMMANDS[command_name]),
            "exit_code": exit_code,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": output,
            "simulated": False,
            "sandbox": evidence,
        }
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name, staging], capture_output=True, timeout=15, env=environment
        )
        subprocess.run(
            ["docker", "image", "rm", "-f", image], capture_output=True, timeout=15, env=environment
        )
