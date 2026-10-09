import pytest
from app.patches import apply_patch, PatchError, safe_path
from app.providers import demo_files, demo_patch
from app.sandbox import run_tests


def test_valid_patch():
    files, changed = apply_patch(demo_patch(), demo_files())
    assert set(changed) == {"auth.py", "tests/test_auth.py"}
    assert 'payload.get("email")' in files["auth.py"]
    assert "test_missing_email" in files["tests/test_auth.py"]


@pytest.mark.parametrize(
    "path",
    [
        "../auth.py",
        "/auth.py",
        "a/../../auth.py",
        ".github/a.py",
        "a\\b.py",
        "a//b.py",
        "auth.txt",
        "a/./b.py",
        ".env",
    ],
)
def test_unsafe_paths(path):
    with pytest.raises(PatchError):
        safe_path(path)


def test_wrong_context():
    with pytest.raises(PatchError):
        apply_patch(demo_patch().replace('payload["email"]', 'payload["wrong"]'), demo_files())


def test_incorrect_hunk_counts():
    with pytest.raises(PatchError):
        apply_patch(demo_patch().replace("@@ -1,3", "@@ -1,8"), demo_files())


def test_command_allowlist():
    with pytest.raises(ValueError):
        run_tests({}, "bash")


def test_missing_docker(monkeypatch):
    monkeypatch.setattr("app.sandbox.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="Docker is required"):
        run_tests(demo_files())


def test_add_file():
    files, changed = apply_patch("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+answer = 42\n", {})
    assert files["new.py"] == "answer = 42\n"


def test_forbidden_rename():
    with pytest.raises(PatchError):
        apply_patch("--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+y\n", {"old.py": "x\n"})
