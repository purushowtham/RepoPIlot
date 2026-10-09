"""Reproduce the bug and verify its fixed patch in actual Docker processes."""
import json

from app.patches import apply_patch
from app.providers import demo_files, demo_patch
from app.sandbox import run_tests

fixed, _ = apply_patch(demo_patch(), demo_files())
baseline = demo_files()
baseline["tests/test_auth.py"] = fixed["tests/test_auth.py"]
failed = run_tests(baseline)
assert failed["exit_code"] == 1 and failed["status"] == "failed"
assert not failed["simulated"]
passed = run_tests(fixed)
print(passed["output"])
assert passed["exit_code"] == 0 and passed["status"] == "passed"
assert not passed["simulated"]
assert passed["sandbox"]["network"] == "none"
assert passed["sandbox"]["read_only"]
assert passed["sandbox"]["user"] == "10001:10001"
print(json.dumps({"baseline_exit_code": failed["exit_code"], "fixed_exit_code": passed["exit_code"], "sandbox": passed["sandbox"]}, indent=2))
