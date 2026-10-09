import base64
import hashlib
import time
from urllib.parse import quote
import httpx
from .config import MODE
from .connections import settings
from .patches import safe_path
from .providers import demo_files


class GitHub:
    def request(self, method, path, **kwargs):
        credential = settings()["github_token"]
        if not credential:
            raise ValueError("Open Live setup and configure GitHub access")
        with httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Authorization": f"Bearer {credential}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

    def metadata(self, name):
        if MODE == "demo":
            if name != "demo/login-service":
                raise ValueError("Demo mode supports demo/login-service only")
            return {"id": 1, "default_branch": "main"}
        return self.request("GET", f"/repos/{name}")

    def snapshot(self, name, ref):
        if MODE == "demo":
            files = demo_files()
            return hashlib.sha256(str(files).encode()).hexdigest()[:40], files
        deadline = time.monotonic() + 120
        commit = self.request("GET", f"/repos/{name}/commits/{quote(ref, safe='')}")
        sha = commit["sha"]
        tree = self.request("GET", f"/repos/{name}/git/trees/{sha}", params={"recursive": "1"})
        if tree.get("truncated"):
            raise ValueError("Repository tree exceeds MVP retrieval budget")
        entries = []
        for entry in tree["tree"]:
            if entry["type"] != "blob" or not entry["path"].endswith(".py"):
                continue
            try:
                safe_path(entry["path"])
            except ValueError:
                continue
            if entry["mode"] not in {"100644", "100755"}:
                raise ValueError("Symlinks are unsupported")
            if entry.get("size", 0) > 20000:
                raise ValueError("Python file exceeds 20KB retrieval limit")
            entries.append(entry)
        if not entries or len(entries) > 60:
            raise ValueError("MVP supports repositories with 1–60 bounded Python files")
        files = {}
        for entry in entries:
            if time.monotonic() > deadline:
                raise ValueError("Repository retrieval exceeded the two-minute budget")
            blob = self.request("GET", f"/repos/{name}/git/blobs/{entry['sha']}")
            source = base64.b64decode(blob["content"]).decode("utf-8")
            if len(source.encode()) > 20000:
                raise ValueError("File exceeds retrieval budget")
            files[entry["path"]] = source
            if sum(len(v.encode()) for v in files.values()) > 500000:
                raise ValueError("Repository exceeds snapshot size budget")
        return sha, files

    def create_pr(self, name, run_id, base_sha, base_ref, files, changed, summary):
        # Caller has checked persisted approval and passing real test evidence.
        branch = f"repopilot/{run_id}"
        existing = self.request(
            "GET", f"/repos/{name}/pulls", params={"state": "all", "head": f"{name.split('/')[0]}:{branch}"}
        )
        if existing:
            return {"url": existing[0]["html_url"], "number": existing[0]["number"], "branch": branch}
        base_commit = self.request("GET", f"/repos/{name}/git/commits/{base_sha}")
        tree = self.request(
            "POST",
            f"/repos/{name}/git/trees",
            json={
                "base_tree": base_commit["tree"]["sha"],
                "tree": [{"path": p, "mode": "100644", "type": "blob", "content": files[p]} for p in changed],
            },
        )
        commit = self.request(
            "POST",
            f"/repos/{name}/git/commits",
            json={"message": f"RepoPilot: {summary[:120]}", "tree": tree["sha"], "parents": [base_sha]},
        )
        try:
            self.request(
                "POST", f"/repos/{name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:
                raise
            current = self.request("GET", f"/repos/{name}/git/ref/heads/{branch}")
            branch_commit = self.request("GET", f"/repos/{name}/git/commits/{current['object']['sha']}")
            if branch_commit["tree"]["sha"] != tree["sha"] or [
                p["sha"] for p in branch_commit["parents"]
            ] != [base_sha]:
                raise ValueError("Existing run branch differs from the approved patch")
        pr = self.request(
            "POST",
            f"/repos/{name}/pulls",
            json={
                "title": f"RepoPilot: {summary[:100]}",
                "head": branch,
                "base": base_ref,
                "draft": True,
                "body": "Human-approved proposed change.\n\n"
                + summary
                + "\n\nValidation: python -m pytest -q passed in the restricted Docker sandbox. Model review is advisory. This MVP supports bounded Python repositories with preinstalled pytest only.",
            },
        )
        return {"url": pr["html_url"], "number": pr["number"], "branch": branch}
