#!/usr/bin/env python3
"""Run the whole suite — the regression gate the chat work lands against.

Every file in tests/ is a standalone script with its own `__main__` block (there is no
pytest dependency in requirements.txt, and pytest is not installed here). So each one is
run the way it was designed to be run: as a subprocess, in its own interpreter.

Subprocess rather than import-and-call on purpose. Importing all 23 modules into one
process would share `sys.modules`, and several tests mutate process state — os.environ
(test_notifications swaps KASH_BOT_TOKEN), kash.llm's module-level `_AVAILABLE` probe
cache, and sqlite connections closed mid-test to force write failures. One test's leftovers
would silently change the next test's result.

    python3 run_tests.py                  # everything
    python3 run_tests.py query readonly    # only files whose name contains these

Exit status is 1 if anything failed, so it works as a pre-commit or CI gate.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")
TIMEOUT = 300           # a hung test must fail the run, not hang it forever


def discover(patterns: list[str]) -> list[str]:
    names = sorted(f for f in os.listdir(TESTS)
                   if f.startswith("test_") and f.endswith(".py"))
    if patterns:
        names = [n for n in names if any(p in n for p in patterns)]
    return names


def main() -> int:
    names = discover(sys.argv[1:])
    if not names:
        print("no matching tests")
        return 1

    failures: list[tuple[str, str]] = []
    started = time.time()
    for name in names:
        path = os.path.join(TESTS, name)
        try:
            proc = subprocess.run([sys.executable, path], capture_output=True,
                                  text=True, timeout=TIMEOUT, cwd=HERE)
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT  {name}")
            failures.append((name, f"exceeded {TIMEOUT}s"))
            continue
        if proc.returncode == 0:
            print(f"  ok       {name}")
        else:
            print(f"  FAIL     {name}")
            failures.append((name, (proc.stdout + proc.stderr).strip()))

    print(f"\n{len(names) - len(failures)}/{len(names)} files passed "
          f"in {time.time() - started:.1f}s")
    for name, detail in failures:
        print(f"\n--- {name} ---\n{detail[-2000:]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
