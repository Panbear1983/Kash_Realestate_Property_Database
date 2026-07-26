"""Pluggable LLM backends for the natural-language ('ask') mode, with a fallback ladder.

Two backends ship today:

* **codex** (default rung of last resort) — drives the local `codex` CLI, which authenticates
  with your ChatGPT subscription over OAuth (no API key, no per-token billing). Each query runs
  in a throwaway isolated working directory with a read-only sandbox and `--output-schema`, so
  Codex returns a structured query spec fast and can't touch anything.
* **anthropic** — the Claude API with native JSON-schema structured outputs. Costs money per
  token, so it sits above codex and is skipped entirely when no key is configured.

`route()` returns a LadderBackend that walks the configured rungs: a rung that is unavailable
(no credential, CLI not logged in, SDK not installed) or that raises is skipped and the next one
answers, so one dead provider no longer takes the whole assistant down. Pinning `backend:` to a
specific name in preferences.yaml disables the ladder and uses exactly that rung — an explicit
choice is honoured strictly, including its failures.

Either way the backend only ever produces a *query spec* (filters/sort/limit); kash.query
executes it locally against the pool. The model never sees a database or writes SQL.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from shutil import which
from typing import Optional

# --- availability cache -------------------------------------------------------------------
# available() used to run on every single question: CodexBackend shells out to `codex login
# status` (a subprocess with a 15s timeout) before the actual `codex exec` call. With a ladder
# that would be one probe per rung per message. Probe results change rarely, so cache them.

_AVAILABLE: dict[str, tuple[bool, str, float]] = {}
DEFAULT_TTL = 60


def _cached(name: str, ttl: int, probe):
    """Run `probe()` at most once per `ttl` seconds per backend name."""
    if ttl <= 0:
        return probe()
    now = time.monotonic()
    hit = _AVAILABLE.get(name)
    if hit is not None and (now - hit[2]) < ttl:
        return hit[0], hit[1]
    ok, why = probe()
    _AVAILABLE[name] = (ok, why, now)
    return ok, why


def _invalidate(name: str, why: str) -> None:
    """Force a rung to read as unavailable until its TTL lapses (fatal errors only)."""
    _AVAILABLE[name] = (False, why, time.monotonic())


def reset_cache() -> None:
    """Drop every cached probe result. Used by tests and after a config change."""
    _AVAILABLE.clear()


def _load_env_file(path: str) -> None:
    """Minimal .env reader — same shape as run_update.py's, no hard dotenv dependency.

    Never overwrites a variable that is already set, so a real environment always wins.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


_ENV_LOADED = False


def _ensure_env() -> None:
    """Resolve credentials from the repo .env, then ~/.hermes/.env (the shared key file every
    repo on this Mac already uses). Mirrors bridge/openrouter_gateway.py's two-tier order."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env_file(os.path.join(here, ".env"))
    _load_env_file(os.path.join(os.path.expanduser("~"), ".hermes", ".env"))
    _ENV_LOADED = True


class CodexBackend:
    name = "codex"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.bin = cfg.get("bin", "codex")
        self.model = cfg.get("model")           # None -> subscription default
        self.timeout = int(cfg.get("timeout", 120))
        self.ttl = int(cfg.get("available_ttl", DEFAULT_TTL))

    def _probe(self) -> tuple[bool, str]:
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

    def available(self) -> tuple[bool, str]:
        return _cached(self.name, self.ttl, self._probe)

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


class ClaudeCliBackend:
    """Claude via the local `claude` CLI — the subscription twin of CodexBackend.

    Authenticates with your Claude subscription (no API key, no per-token billing), and
    `--json-schema` constrains the reply to the same schema kash hands every other backend.

    Runs with a throwaway settings file that denies every tool and `--strict-mcp-config` so no
    MCP servers load, in a temp cwd so it cannot see the repo. That is sandboxing only — it does
    *not* meaningfully cut cost. Each cold call carries ~17.5k cache-creation tokens of Claude
    Code preamble whether tools are stripped or not (measured both ways; an earlier apparent
    saving turned out to be a prompt-cache hit from the preceding test run). Consecutive calls
    inside the cache TTL are ~10x cheaper, so bursts of questions are far cheaper than the first.

    On a Claude subscription that overhead is quota rather than dollars, but it is the reason
    this rung sits below codex in the default ladder rather than above it.
    """

    name = "claude_cli"

    _DENY = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
             "Task", "TodoWrite", "NotebookEdit"]

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.bin = cfg.get("bin", "claude")
        self.model = cfg.get("model", "haiku")   # haiku is ample for emitting a query spec
        self.timeout = int(cfg.get("timeout", 120))
        self.ttl = int(cfg.get("available_ttl", DEFAULT_TTL))
        self.last_usage: Optional[dict] = None

    def _probe(self) -> tuple[bool, str]:
        # The CLI has no cheap "am I logged in" check, so presence is all we verify — an
        # expired login surfaces as a failed call and the ladder falls through from there.
        if not which(self.bin):
            return False, "claude CLI not found (install Claude Code, then: claude)"
        return True, ""

    def available(self) -> tuple[bool, str]:
        return _cached(self.name, self.ttl, self._probe)

    def query_spec(self, prompt: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory() as wd:
            settings_path = os.path.join(wd, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"permissions": {"allow": [], "deny": self._DENY}}, f)
            cmd = [self.bin, "-p", prompt,
                   "--json-schema", json.dumps(schema),
                   "--output-format", "json",
                   "--settings", settings_path,
                   "--strict-mcp-config"]
            if self.model:
                cmd += ["--model", self.model]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout,
                               cwd=wd, stdin=subprocess.DEVNULL)

        line = next((ln for ln in r.stdout.splitlines() if ln.lstrip().startswith("{")), "")
        if not line:
            raise RuntimeError(f"claude produced no output: {(r.stderr or r.stdout)[:120]}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude returned non-JSON: {line[:120]}") from e

        if payload.get("is_error"):
            raise RuntimeError(f"claude error: {str(payload.get('result'))[:150]}")

        usage = payload.get("usage") or {}
        self.last_usage = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cost_usd": payload.get("total_cost_usd"),
        }

        # --json-schema gives us the parsed object directly; `result` is the same thing as a
        # string, kept as a fallback in case a future CLI version drops the parsed field.
        spec = payload.get("structured_output")
        if spec is None:
            try:
                spec = json.loads(payload.get("result") or "")
            except (json.JSONDecodeError, TypeError) as e:
                raise RuntimeError("claude returned no structured output") from e
        return spec


def _loads_loose(text: str):
    """Parse JSON that may arrive wrapped in prose or a markdown fence.

    Needed only for backends that cannot hard-constrain their output. Returns None rather
    than raising, so the caller can decide whether to retry.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        body = text.split("```")[1] if text.count("```") >= 2 else text.strip("`")
        text = body.split("\n", 1)[1] if body.lower().startswith(("json\n", "json\r")) else body
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


class AgyCliBackend:
    """Gemini via the Antigravity CLI (`agy`) — the third subscription rung.

    Runs on the Gemini Pro subscription's OAuth quota, no API key. This is the same binary
    Investment_Strategy_Research_2026 drives (`cli_command: agy`); the classic `gemini` CLI is
    NOT usable here — Google sunset its free Code Assist tier and it now fails auth before it
    ever reads a key.

    Unlike codex (`--output-schema`) and claude (`--json-schema`), `agy` has no way to hard
    constrain output, so the schema goes in the prompt and the reply is parsed leniently with a
    retry. That makes this the least reliable rung for structured work, which — together with
    its ~19s latency — is why it belongs last in a ladder and off the chat path entirely.
    """

    name = "agy_cli"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.bin = cfg.get("bin", "agy")
        self.model = cfg.get("model", "gemini-3.5-flash-low")
        self.timeout = int(cfg.get("timeout", 180))
        self.ttl = int(cfg.get("available_ttl", DEFAULT_TTL))
        self.retries = int(cfg.get("retries", 2))

    def _probe(self) -> tuple[bool, str]:
        if not which(self.bin):
            return False, "agy CLI not found (Antigravity; see ~/.gemini/antigravity-cli)"
        return True, ""

    def available(self) -> tuple[bool, str]:
        return _cached(self.name, self.ttl, self._probe)

    def query_spec(self, prompt: str, schema: dict) -> dict:
        instruction = (
            "Output ONLY a single JSON object conforming to this JSON Schema. "
            "No prose, no explanation, no markdown code fence.\n"
            f"Schema: {json.dumps(schema)}\n\n"
        )
        last = ""
        for attempt in range(max(1, self.retries)):
            body = instruction + prompt
            if attempt:
                body = ("Your previous reply was not valid JSON. Return the JSON object only, "
                        "starting with { and ending with }.\n\n" + body)
            cmd = [self.bin, "-p", body, "--mode", "plan"]
            if self.model:
                cmd += ["--model", self.model]
            with tempfile.TemporaryDirectory() as wd:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=self.timeout, cwd=wd, stdin=subprocess.DEVNULL)
            spec = _loads_loose(r.stdout)
            if isinstance(spec, dict):
                return spec
            last = (r.stdout or r.stderr or "empty output").strip()[:150]
        raise RuntimeError(f"agy returned unparseable JSON after {self.retries} tries: {last}")


class AnthropicBackend:
    """Claude API rung. Uses native structured outputs, so the model is constrained to the same
    JSON Schema kash already hands Codex — no prompt-level 'please return JSON' pleading.

    The SDK is imported lazily: kash.llm is imported by the Telegram bridge and the dashboard,
    and a missing `anthropic` package must make this rung unavailable, not break every caller.
    """

    name = "anthropic"

    # Errors that mean "this rung is misconfigured" rather than "try again" — on these we mark
    # the rung unavailable so the rest of the TTL window skips it without another API round trip.
    FATAL = ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError")

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.model = cfg.get("model") or "claude-haiku-4-5"
        self.max_tokens = int(cfg.get("max_tokens", 1024))
        self.timeout = int(cfg.get("timeout", 60))
        self.ttl = int(cfg.get("available_ttl", DEFAULT_TTL))
        self.last_usage: Optional[dict] = None

    @staticmethod
    def _api_key() -> str:
        _ensure_env()
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def _probe(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK not installed (pip install anthropic)"
        if not self._api_key():
            return False, "ANTHROPIC_API_KEY not set (.env or ~/.hermes/.env)"
        return True, ""

    def available(self) -> tuple[bool, str]:
        return _cached(self.name, self.ttl, self._probe)

    def query_spec(self, prompt: str, schema: dict) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key(), timeout=self.timeout)
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001 — classified by the ladder, not swallowed
            if type(e).__name__ in self.FATAL:
                _invalidate(self.name, f"{type(e).__name__}: {e}")
            raise

        # Check stop_reason before touching content: a refusal returns HTTP 200 with empty or
        # partial content, so indexing content[0] blindly would raise something misleading.
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("anthropic declined the request")

        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }

        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        if not text:
            raise RuntimeError(f"anthropic returned no text (stop_reason={resp.stop_reason})")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"anthropic returned non-JSON: {text[:120]}") from e


BACKENDS = {
    "codex": CodexBackend,             # ChatGPT subscription via the codex CLI
    "claude_cli": ClaudeCliBackend,    # Claude subscription via the claude CLI
    "agy_cli": AgyCliBackend,          # Gemini Pro subscription via the Antigravity CLI
    "anthropic": AnthropicBackend,     # Claude API — needs ANTHROPIC_API_KEY, billed per token
}


def backend_config(cfg: Optional[dict], name: str) -> dict:
    """Per-backend settings, inheriting the shared timeout/ttl from the top of the llm block.

    Also understands the pre-ladder flat form (`llm: {backend: codex, model: null,
    timeout: 120}`), where `model` applies to whichever single backend was selected.
    """
    cfg = cfg or {}
    merged = {
        "timeout": cfg.get("timeout", 120),
        "available_ttl": cfg.get("available_ttl", DEFAULT_TTL),
    }
    per = (cfg.get("backends") or {}).get(name) or {}
    if not per and cfg.get("backend") == name and "model" in cfg:
        merged["model"] = cfg.get("model")
    merged.update(per)
    return merged


def resolve_ladder(cfg: Optional[dict], override: Optional[str] = None,
                   job: Optional[str] = None) -> list[str]:
    """Which rungs to try, in order.

    An explicit choice (a per-user override, or `backend:` set to something other than `auto`)
    pins exactly one rung — pinning is honoured strictly so that `backend: codex` reproduces the
    pre-ladder behaviour byte for byte, failures included.

    Otherwise the ladder is chosen per job (`ladders:` in preferences.yaml), because the jobs
    have opposite priorities: `chat` has a user waiting so latency wins, while `rank` is a
    nightly judgment call where depth matters and 19 seconds is irrelevant. An unnamed or
    unconfigured job falls back to the shared `ladder`.
    """
    cfg = cfg or {}
    choice = (override or cfg.get("backend") or "codex")
    choice = str(choice).strip().lower()
    if choice and choice != "auto":
        if choice not in BACKENDS:
            raise ValueError(
                f"unknown LLM backend '{choice}' (this build implements: "
                f"{', '.join(sorted(BACKENDS))})"
            )
        return [choice]
    names = None
    if job:
        names = (cfg.get("ladders") or {}).get(job)
    if names is None:
        names = cfg.get("ladder") or ["codex", "claude_cli"]
    ladder = [n for n in names if n in BACKENDS]
    return ladder or ["codex"]


class LadderBackend:
    """Tries each rung in turn; the first that is available *and* answers wins.

    Satisfies the same two-method contract as a single backend, so kash.nl and kash.rank cannot
    tell the difference. After a successful call, `chosen` names the rung that produced the spec.
    """

    def __init__(self, cfg: Optional[dict], names: list[str]):
        self._cfg = cfg or {}
        self.names = list(names)
        self.name = "/".join(self.names) or "none"
        self.chosen: Optional[str] = None
        self.last_usage: Optional[dict] = None

    def _rungs(self):
        for n in self.names:
            yield n, BACKENDS[n](backend_config(self._cfg, n))

    def available(self) -> tuple[bool, str]:
        reasons = []
        for n, backend in self._rungs():
            ok, why = backend.available()
            if ok:
                return True, ""
            reasons.append(f"{n}: {why}")
        return False, "; ".join(reasons)

    def query_spec(self, prompt: str, schema: dict) -> dict:
        problems = []
        for n, backend in self._rungs():
            ok, why = backend.available()
            if not ok:
                problems.append(f"{n}: {why}")
                continue
            try:
                spec = backend.query_spec(prompt, schema)
            except Exception as e:  # noqa: BLE001 — a dead rung must not end the request
                problems.append(f"{n}: {e}")
                continue
            self.chosen = n
            self.last_usage = getattr(backend, "last_usage", None)
            return spec
        raise RuntimeError("every LLM backend failed — " + "; ".join(problems))


def route(config: Optional[dict] = None, override: Optional[str] = None,
          job: Optional[str] = None, actor=None, store=None) -> LadderBackend:
    """Build the backend for a request.

    `config` is the `llm:` block from preferences.yaml. `job` selects a named ladder
    (`chat` / `extract` / `rank`); `override` pins one rung and wins over everything.

    When `actor` and `store` are given, rungs this actor has already exhausted today are moved
    to the back of the ladder rather than removed. Every rung is a separate subscription, so a
    heavy user rolls onto a different provider's quota instead of draining one — and because
    exhausted rungs are demoted rather than dropped, running out everywhere degrades to the
    original order instead of refusing to answer.

    An explicit override still wins: pinning is an instruction, not a suggestion.
    """
    names = resolve_ladder(config, override, job)
    if actor is not None and store is not None and not override and len(names) > 1:
        try:
            from .usage import Usage, within_budget
            usage = Usage(store)
            fresh = [n for n in names if within_budget(config, usage, actor, n)]
            spent = [n for n in names if n not in fresh]
            if fresh:
                names = fresh + spent
        except Exception:  # noqa: BLE001 — never let accounting break routing
            pass
    return LadderBackend(config, names)


def get_backend(config: Optional[dict] = None):
    """Backward-compatible factory — kash.rank calls this with `prefs['llm']`.

    Delegates to route() so existing callers get the ladder without a change, and so
    `backend: auto` doesn't read as an unknown backend name. Still raises ValueError on a
    genuinely unknown name, as it always did.
    """
    return route(config)
