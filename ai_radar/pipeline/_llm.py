"""LLM backend abstraction.

Two backends, one ``complete(prompt) -> str`` contract:

* ``KimiCLIBackend``  — subprocess wrapper around the local ``kimi-cli`` agent.
  Used for cheap, simple tasks (prefilter). Slow (~30s startup) but free.
* ``DeepSeekBackend`` — OpenAI-compatible HTTP client for ``api.deepseek.com``.
  Used for the world-knowledge-strong scoring step (Iron Law A: 评分别省钱).

The contract is intentionally ``str -> str``. Higher layers wrap their
prompt up however they like and parse the model's reply.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Protocol

import httpx

# ---------- shared response helpers ----------

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_RESUME_LINE_RE = re.compile(r"^To resume this session:.*$", re.MULTILINE)
# Kimi server-side content moderation reject. Surfaces in kimi-cli stdout as
# ``Error code: 400 ... 'type': 'content_filter'`` and from the HTTP API as a
# 400 body with the same ``type``. Permanent for that exact prompt — do NOT
# retry the same request; bisect the batch instead.
_CONTENT_FILTER_RE = re.compile(
    r"content_filter|considered high risk", re.IGNORECASE
)


class KimiContentFilterError(RuntimeError):
    """Kimi rejected the prompt as 'high risk'. Don't retry — bisect the batch."""


def extract_json(raw: str) -> object:
    """Parse a JSON value out of an LLM reply.

    Handles three shapes:
    1. Plain JSON in the whole response.
    2. JSON inside ```json ... ``` (or just ``` ... ```).
    3. JSON array buried in surrounding text — finds the first balanced [...].
    """
    text = _RESUME_LINE_RE.sub("", raw).strip()

    m = _CODE_BLOCK_RE.search(text)
    if m:
        return json.loads(m.group(1).strip())

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    if start == -1:
        start = text.find("{")
    if start == -1:
        raise ValueError("no JSON found in response")
    depth = 0
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    for i in range(start, len(text)):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON")


# ---------- backends ----------

class LLMBackend(Protocol):
    name: str

    def complete(self, prompt: str) -> str: ...


class KimiCLIBackend:
    name = "kimi-cli"

    def __init__(
        self,
        cwd: str,
        *,
        bin_path: str = "kimi-cli",
        timeout_s: int = 180,
        thinking: bool = False,
    ) -> None:
        self.cwd = cwd
        self.bin_path = bin_path
        self.timeout_s = timeout_s
        self.thinking = thinking

    # Transient rc=1 patterns we should retry. kimi-cli prints a session ID
    # ("To resume this session: kimi -r ...") on most internal errors —
    # auth refresh blips, brief net flaps, agent-loop guard trips. Retrying
    # the whole subprocess invocation usually clears them; persisting the
    # exact session is rarely useful for our stateless JSON-batch workload.
    _MAX_RETRIES = 2  # one initial attempt + up to 2 retries = 3 tries total

    def complete(self, prompt: str) -> str:
        # --max-steps-per-turn 1: force single-step inference, no agent
        # tool-calling loop. kimi-cli's default config sets this to 100;
        # for pure JSON-output use cases (prefilter / score) the agent
        # has nothing useful to do, but it still consumes ~30-90s probing
        # the workspace before answering. Capping to 1 makes kimi behave
        # like a plain LLM API call — comparable speed to deepseek-v4-flash.
        cmd = [
            self.bin_path,
            "-p", prompt,
            "--quiet", "--afk", "-y",
            "--max-steps-per-turn", "1",
        ]
        cmd.append("--thinking" if self.thinking else "--no-thinking")

        last_err: str = ""
        for attempt in range(self._MAX_RETRIES + 1):
            proc = subprocess.run(
                cmd,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout
            combined = ((proc.stdout or "") + "\n" + (proc.stderr or ""))
            if _CONTENT_FILTER_RE.search(combined):
                # Permanent for this exact prompt — caller should bisect.
                raise KimiContentFilterError(combined.strip()[:500])
            last_err = (proc.stderr or "").strip()
            if os.environ.get("RADAR_KIMI_DEBUG"):
                import sys as _sys
                _sys.stderr.write(
                    f"\n[kimi-debug attempt {attempt+1}] rc={proc.returncode}\n"
                    f"stderr={last_err!r}\nstdout_tail={(proc.stdout or '')[-500:]!r}\n"
                )
            last_err = last_err[:1500]
            if attempt < self._MAX_RETRIES:
                # Brief backoff before retry. Exponential: 1.5s, 3s.
                import time as _t
                _t.sleep(1.5 * (attempt + 1))
        raise RuntimeError(
            f"kimi-cli rc={proc.returncode} after {self._MAX_RETRIES + 1} attempts: {last_err}"
        )


class KimiCLIPersistentBackend:
    """Single long-lived kimi-cli print-mode session, reused across calls.

    kimi-cli ``--print --input-format stream-json --output-format stream-json``
    accepts JSON messages on stdin and emits JSON responses on stdout. Each
    user message of the form ``{"role":"user","content":[{"type":"text","text":"..."}]}``
    triggers one inference; the slash command ``/clear`` (sent as a regular
    user message in stream-json mode) wipes the LLM context without restarting
    the subprocess.

    Why this matters vs ``KimiCLIBackend``:
      - Spawning kimi-cli costs ~1-3s for OAuth/skills/config load. With 30
        items at batch_size=3, that's 10 spawns per ``radar score`` run.
      - Each spawn also has its own chance of transient ``rc=1`` (auth refresh
        blip, etc.). One persistent session = one chance of that failure
        instead of N.
      - ``/clear`` between prompts keeps each batch independent (no leaking
        context from the previous batch into scoring decisions).

    Lifecycle: subprocess spawned lazily on first ``complete()``. If the
    subprocess dies (BrokenPipe / EOF), the next ``complete()`` respawns it.
    Caller may invoke ``close()`` to clean up explicitly; otherwise it dies
    with the parent process.
    """

    name = "kimi-cli-persistent"

    def __init__(
        self,
        cwd: str,
        *,
        bin_path: str = "kimi-cli",
        timeout_s: int = 120,
        thinking: bool = False,
    ) -> None:
        self.cwd = cwd
        self.bin_path = bin_path
        self.timeout_s = timeout_s
        self.thinking = thinking
        self._proc: subprocess.Popen | None = None
        self._call_count: int = 0  # 0 => first call this session, no /clear needed

    def _spawn(self) -> None:
        cmd = [
            self.bin_path,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--afk", "-y",
            "--max-steps-per-turn", "1",
            "--final-message-only",
            "--thinking" if self.thinking else "--no-thinking",
        ]
        self._proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._call_count = 0

    def _ensure_alive(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()

    def _send(self, text: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        msg = json.dumps(
            {"role": "user", "content": [{"type": "text", "text": text}]},
            ensure_ascii=False,
        )
        self._proc.stdin.write(msg + "\n")
        self._proc.stdin.flush()

    def _read_assistant(self) -> str:
        """Block until one assistant JSON line arrives; return content."""
        assert self._proc is not None and self._proc.stdout is not None
        import time as _t
        deadline = _t.time() + self.timeout_s
        while _t.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("kimi-cli stdout EOF (subprocess died)")
            line = line.strip()
            if not line:
                continue
            if _CONTENT_FILTER_RE.search(line):
                raise KimiContentFilterError(line[:500])
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Free-text noise (banners, "To resume this session: ..."
                # lines etc.). Skip.
                continue
            if obj.get("role") != "assistant":
                continue
            content = obj.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Concatenate text parts.
                return "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            continue
        raise RuntimeError(
            f"kimi-cli no assistant response within {self.timeout_s}s"
        )

    def complete(self, prompt: str) -> str:
        # Two attempts: first on the (possibly stale) session, second after
        # forced respawn. Catches subprocess-died mid-stream blips without
        # bubbling the user a confusing error.
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure_alive()
                if self._call_count > 0:
                    # Wipe LLM context between batches — each scoring/prefilter
                    # batch must be independent.
                    self._send("/clear")
                    self._read_assistant()  # absorb "The context has been cleared."
                self._send(prompt)
                out = self._read_assistant()
                self._call_count += 1
                return out
            except KimiContentFilterError:
                # Permanent for this prompt. Don't respawn / retry; bubble up
                # so the caller can bisect. Kill the proc anyway because
                # kimi-cli's stream-json state machine may be wedged after a
                # mid-stream rejection — next call will respawn cleanly.
                self._kill_proc()
                raise
            except (BrokenPipeError, OSError, RuntimeError) as e:
                last_err = e
                # Subprocess died mid-call. Force respawn next iteration.
                self._kill_proc()
        assert last_err is not None
        raise RuntimeError(f"kimi-cli persistent session failed twice: {last_err}")

    def _kill_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def close(self) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._kill_proc()
        self._proc = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class KimiAPIBackend:
    """OpenAI-compatible chat completions against Kimi for Coding API.

    Same model as the kimi-cli subprocess (kimi-for-coding), but a direct HTTP
    call → no subprocess startup, no agent loop, no skill discovery. ~10-30×
    faster for pure JSON-output workloads (prefilter + score) on the same
    OAuth credentials kimi-cli uses.

    Auth model: the ``sk-kimi-*`` token IS the OAuth bearer that kimi-cli
    fetches and caches in ``~/.kimi/credentials/kimi-code.json``. The
    ``api.kimi.com/coding/v1`` endpoint enforces a client-identity check on
    top of bearer auth, expecting the same headers kimi-cli sends:
    ``User-Agent: KimiCLI/<ver>`` plus a set of ``X-Msh-*`` device-info
    headers. We mimic those so the server treats this client the same way.

    Caveat: this is the user's own credential talking to the same endpoint
    they'd hit via kimi-cli, just without the agent overhead. If Moonshot
    later changes their client-identity check (TLS pinning, request signing),
    this stops working — fall back to ``KimiCLIBackend``.

    Required env: ``KIMI_API_KEY``.
    Optional env: ``KIMI_API_BASE`` (default https://api.kimi.com/coding/v1),
                  ``KIMI_MODEL``    (default kimi-for-coding).
    """

    DEFAULT_BASE = "https://api.kimi.com/coding/v1"
    DEFAULT_MODEL = "kimi-for-coding"
    API_KEY_VARS = ("KIMI_API_KEY",)
    # Hardcoded fallback if `kimi-cli --version` not available on PATH.
    # Bumping this occasionally is fine; server only checks it loosely.
    KIMI_CLI_VERSION_FALLBACK = "1.40.0"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
        temperature: float = 0.2,
    ) -> None:
        if api_key is None:
            for var in self.API_KEY_VARS:
                api_key = os.environ.get(var)
                if api_key:
                    break
        self.api_key = api_key
        if not self.api_key:
            raise RuntimeError(
                f"No API key found. Set one of {self.API_KEY_VARS} in env or .env."
            )
        self.base_url = (base_url or os.environ.get("KIMI_API_BASE")
                         or self.DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("KIMI_MODEL") or self.DEFAULT_MODEL
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.name = self.model
        self._headers = self._build_headers()

    def _build_headers(self) -> dict[str, str]:
        import platform as _pl
        import socket as _so
        ver = self._detect_kimi_cli_version()
        system = _pl.system()
        if system == "Darwin":
            mac = _pl.mac_ver()[0] or _pl.release()
            arch = _pl.machine() or ""
            device_model = f"macOS {mac} {arch}".strip()
        elif system == "Linux":
            device_model = f"Linux {_pl.machine() or 'unknown'}"
        else:
            device_model = f"{system} {_pl.machine() or 'unknown'}"
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"KimiCLI/{ver}",
            "X-Msh-Platform": "kimi_cli",
            "X-Msh-Version": ver,
            "X-Msh-Device-Name": _pl.node() or _so.gethostname() or "unknown",
            "X-Msh-Device-Model": device_model,
            "X-Msh-Os-Version": _pl.version() or "unknown",
            "X-Msh-Device-Id": self._persistent_device_id(),
        }
        return {k: self._ascii_safe(v) for k, v in h.items()}

    @staticmethod
    def _ascii_safe(value: str, fallback: str = "unknown") -> str:
        try:
            value.encode("ascii")
            return value.strip()
        except UnicodeEncodeError:
            s = value.encode("ascii", errors="ignore").decode("ascii").strip()
            return s or fallback

    @classmethod
    def _detect_kimi_cli_version(cls) -> str:
        """Best-effort: parse ``kimi-cli --version``. Cached on the class."""
        cached = getattr(cls, "_cached_kimi_ver", None)
        if cached:
            return cached
        try:
            import subprocess as _sp
            r = _sp.run(["kimi-cli", "--version"], capture_output=True,
                        text=True, timeout=3)
            m = re.search(r"(\d+\.\d+\.\d+)", (r.stdout or "") + (r.stderr or ""))
            cls._cached_kimi_ver = m.group(1) if m else cls.KIMI_CLI_VERSION_FALLBACK
        except Exception:
            cls._cached_kimi_ver = cls.KIMI_CLI_VERSION_FALLBACK
        return cls._cached_kimi_ver  # type: ignore[return-value]

    @staticmethod
    def _persistent_device_id() -> str:
        """Stable random UUID per host, persisted under XDG cache. Generated
        once and reused — server sees a consistent device id across runs."""
        import uuid
        from pathlib import Path
        cache_root = Path(os.environ.get("XDG_CACHE_HOME") or
                          (Path.home() / ".cache"))
        path = cache_root / "ai-radar" / "kimi-device-id"
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        new = str(uuid.uuid4())
        path.write_text(new)
        return new

    def complete(self, prompt: str) -> str:
        # Kimi for Coding endpoint expects stream=True (matches what kimi-cli
        # sends via kosong.chat_provider.kimi). Non-stream requests routinely
        # time out for >1KB outputs — likely the backend isn't optimised for
        # that path. We collect the SSE chunks and return concatenated text.
        with httpx.Client(timeout=self.timeout_s) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    body = resp.text or ""
                    if resp.status_code == 400 and _CONTENT_FILTER_RE.search(body):
                        raise KimiContentFilterError(body[:500])
                    raise RuntimeError(
                        f"kimi HTTP {resp.status_code}: {body[:300]}"
                    )
                chunks: list[str] = []
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].lstrip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = obj["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError):
                        continue
                    if delta:
                        chunks.append(delta)
        if not chunks:
            raise RuntimeError("kimi: empty stream response")
        return "".join(chunks)


class DeepSeekBackend:
    """OpenAI-compatible chat completions against api.deepseek.com.

    Required env: ``DEEPSEEK_API_KEY``.
    Optional env: ``DEEPSEEK_API_BASE`` (default https://api.deepseek.com),
                  ``DEEPSEEK_MODEL``    (default deepseek-v4-pro).

    DeepSeek model line-up (2026-05):
      - ``deepseek-v4-pro``    — flagship; what scoring should use (Iron Law A).
      - ``deepseek-v4-flash``  — cheaper, faster; OK for prefilter, not scoring.
      - ``deepseek-chat`` / ``deepseek-reasoner`` — DEPRECATING 2026-07-24.
    """

    DEFAULT_BASE = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"

    # Names checked in order. DPSK_API is the user's existing convention.
    API_KEY_VARS = ("DEEPSEEK_API_KEY", "DPSK_API")

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
        temperature: float = 0.2,
    ) -> None:
        if api_key is None:
            for var in self.API_KEY_VARS:
                api_key = os.environ.get(var)
                if api_key:
                    break
        self.api_key = api_key
        if not self.api_key:
            raise RuntimeError(
                f"No API key found. Set one of {self.API_KEY_VARS} in env or .env. "
                f"Get a key at https://platform.deepseek.com."
            )
        self.base_url = (base_url or os.environ.get("DEEPSEEK_API_BASE")
                         or self.DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or self.DEFAULT_MODEL
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.name = self.model

    def complete(self, prompt: str) -> str:
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(
                f"{self.base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "stream": False,
                },
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"deepseek HTTP {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"deepseek malformed response: {body!r}") from exc
