import re
from pathlib import PurePosixPath
from .config import MAX_PATCH_BYTES, MAX_FILES


class PatchError(ValueError):
    pass


def safe_path(path):
    parts = PurePosixPath(path).parts
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in {".", ".."} or p.startswith(".") for p in path.split("/"))
    ):
        raise PatchError("Unsafe file path")
    if (
        str(PurePosixPath(path)) != path
        or not path.endswith(".py")
        or any(p in {"node_modules", "venv", "__pycache__"} for p in parts)
    ):
        raise PatchError("MVP patches support visible Python source and test files only")
    return path


def apply_patch(diff, files):
    if len(diff.encode()) > MAX_PATCH_BYTES:
        raise PatchError("Patch exceeds size limit")
    lines = diff.splitlines(keepends=True)
    result = dict(files)
    changed = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git ") or lines[i].startswith("index "):
            i += 1
            continue
        if not lines[i].startswith("--- ") or i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise PatchError("Expected unified diff file headers; binary, modes and renames are unsupported")
        old = lines[i][4:].strip()
        new = lines[i + 1][4:].strip()
        if not new.startswith("b/") or (old != "/dev/null" and old != "a/" + new[2:]):
            raise PatchError("File deletion and renaming are unsupported")
        path = safe_path(new[2:])
        if path in changed or len(changed) >= MAX_FILES:
            raise PatchError("Duplicate file or too many changed files")
        if (old == "/dev/null" and path in files) or (old != "/dev/null" and path not in files):
            raise PatchError("Patch base does not match snapshot")
        original = files.get(path, "").splitlines(keepends=True)
        out = []
        cursor = 0
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@ "):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", lines[i])
            if not match:
                raise PatchError("Invalid hunk header")
            start, count, new_start, new_count = (int(x) if x is not None else 1 for x in match.groups())
            offset = start - 1 if count else start
            if offset < cursor or offset > len(original):
                raise PatchError("Invalid hunk position")
            out.extend(original[cursor:offset])
            if (new_start - 1 if new_count else new_start) != len(out):
                raise PatchError("New hunk offset does not match")
            cursor = offset
            read = added = 0
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@ ", "--- ", "diff --git ")):
                line = lines[i]
                if line.startswith("\\ No newline"):
                    raise PatchError("No-newline markers are unsupported; use newline-terminated files")
                if not line or line[0] not in " +-":
                    raise PatchError("Invalid hunk content")
                kind, content = line[0], line[1:]
                if kind in " -":
                    if cursor >= len(original) or original[cursor] != content:
                        raise PatchError("Patch context does not match pinned source")
                    cursor += 1
                    read += 1
                if kind in " +":
                    out.append(content)
                    added += 1
                i += 1
            if read != count or added != new_count:
                raise PatchError("Hunk counts do not match")
            hunks += 1
        if not hunks:
            raise PatchError("Empty file patch")
        out.extend(original[cursor:])
        result[path] = "".join(out)
        if result[path] == files.get(path):
            raise PatchError("Patch makes no change")
        changed.append(path)
    if not changed:
        raise PatchError("Empty patch")
    if sum(len(v.encode()) for v in result.values()) > 500000:
        raise PatchError("Snapshot exceeds sandbox budget")
    return result, changed
