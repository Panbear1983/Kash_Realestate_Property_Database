"""Pluggable LLM backend for the natural-language ('ask') mode.

Default backend is **Codex** — it drives the local `codex` CLI, which authenticates with
your ChatGPT subscription over OAuth (no API key, no per-token billing). Each query runs
in a throwaway isolated working directory with a read-only sandbox and `--output-schema`,
so Codex returns a structured query spec fast and can't touch anything.

The backend only ever produces a *query spec* (filters/sort/limit); kash.query executes
it locally against the pool. The model never sees a database or writes SQL.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from shutil import which
from typing import Optional


class CodexBackend:
    name = "codex"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.bin = cfg.get("bin", "codex")
        self.model = cfg.get("model")           # None -> subscription default
        self.timeout = int(cfg.get("timeout", 120))

    def available(self) -> tuple[bool, str]:
        if not which(self.bin):
            return False, "codex CLI not found (install it, then: codex login)"
        try:
            r = subprocess.run([self.bin, "login", "status"],
                               capture_output=True, text=True, timeout=15)
            if "Logged in" in (r.stdout + r.stderr):
                return True, ""
            return False, "codex not logged in (run: codex login)"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def query_spec(self, prompt: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory() as wd:
            schema_path = os.path.join(wd, "schema.json")
            out_path = os.path.join(wd, "out.json")
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(schema, f)
            cmd = [self.bin, "exec", "-C", wd, "--skip-git-repo-check",
                   "--ignore-user-config", "-s", "read-only",
                   "--output-schema", schema_path, "--output-last-message", out_path]
            if self.model:
                cmd += ["-m", self.model]
            cmd += [prompt]
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=self.timeout, cwd=wd)
            if not os.path.exists(out_path):
                raise RuntimeError("codex produced no output (check: codex login)")
            text = open(out_path, encoding="utf-8").read().strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"codex returned non-JSON: {text[:120]}") from e


def get_backend(config: Optional[dict] = None):
    cfg = config or {}
    name = cfg.get("backend", "codex")
    if name == "codex":
        return CodexBackend(cfg)
    raise ValueError(f"unknown LLM backend '{name}' (this build implements: codex)")
