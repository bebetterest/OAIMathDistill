"""Codex CLI and OpenAI-compatible chat HTTP adapters; no SDK dependency."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
import signal
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from .core import check_keys, positive, require_text, save_json


class BackendError(RuntimeError):
    pass


class CodexBackend:
    kind = "codex"
    supports_files = True

    def __init__(self, config: dict):
        check_keys(config, {"kind", "command", "model", "profile", "timeout", "overrides"}, "Codex backend")
        positive(config.get("timeout", 600), "Codex timeout")
        for name in ("model", "profile"):
            if name in config:
                require_text(config[name], f"Codex {name}")
        overrides = config.get("overrides", {})
        check_keys(overrides, {"model_reasoning_effort", "model_verbosity"}, "Codex overrides")
        for name, value in overrides.items():
            require_text(value, f"Codex override {name}")
        self.config = config
        command = config.get("command", ["codex"])
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError("Codex command must be a nonempty argv list")
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            processes = list(self._processes)
        for process in processes:
            self._stop(process)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass

    def complete(self, prompt: str, schema: dict, folder: Path, cwd: Path) -> dict:
        schema_file, answer = folder / "schema.json", folder / "answer.json"
        save_json(schema_file, schema)
        command = self.config.get("command", ["codex"])
        if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
            raise ValueError("Codex command must be a nonempty argv list")
        args = [*command, "exec", "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never",
                "--cd", str(cwd), "--output-schema", str(schema_file), "--output-last-message", str(answer)]
        for key in ("model", "profile"):
            if self.config.get(key):
                args.extend([f"--{key}", self.config[key]])
        for key, value in self.config.get("overrides", {}).items():
            if key not in {"model_reasoning_effort", "model_verbosity"}:
                raise ValueError(f"Unsupported Codex model override: {key}")
            args.extend(["-c", f"{key}={json.dumps(value)}"])
        args.append("-")
        child_environment = os.environ.copy()
        removed = []
        # A fresh CLI must not inherit the desktop application's routing identity.
        if "CODEX_INTERNAL_ORIGINATOR_OVERRIDE" in child_environment:
            child_environment.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
            removed.append("CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
        save_json(folder / "invocation.json", {"argv": args, "requested_model": self.config.get("model", "inherited"),
                                               "profile": self.config.get("profile", "inherited"),
                                               "removed_inherited_environment": removed})
        with (folder / "events.jsonl").open("w") as events, (folder / "stderr.txt").open("w") as stderr:
            with self._lock:
                if self._closed:
                    raise BackendError("Codex backend is closed; no further calls can start")
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=events, stderr=stderr,
                                           text=True, start_new_session=True, cwd=cwd, env=child_environment)
                self._processes.add(process)
            try:
                process.communicate(prompt, timeout=self.config.get("timeout", 600))
            except BaseException:
                self._stop(process)
                raise
            finally:
                with self._lock:
                    self._processes.discard(process)
        usage = []
        completed = False
        failed = False
        for line in (folder / "events.jsonl").read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.failed":
                failed = True
            if event.get("type") == "turn.completed":
                completed = True
            if event.get("usage"):
                usage.append(event["usage"])
        save_json(folder / "usage.json", usage)
        if process.returncode:
            raise BackendError(f"Codex exited {process.returncode}; see {folder / 'stderr.txt'}")
        if failed:
            raise BackendError("Codex event reports failure: turn.failed")
        if not completed:
            raise BackendError("Codex returned no completed turn event")
        if not answer.is_file():
            raise BackendError("Codex returned no final answer file")
        return json.loads(answer.read_text())


class ChatBackend:
    kind = "chat"
    supports_files = False

    def __init__(self, config: dict):
        check_keys(config, {"kind", "base_url", "model", "timeout", "max_tokens", "token_field",
                            "temperature", "json_mode", "api_key_env", "env_file", "extra"}, "chat backend")
        positive(config.get("timeout", 120), "chat timeout")
        positive(config.get("max_tokens", 8192), "max_tokens", integer=True)
        require_text(config.get("model"), "Chat backend explicit model")
        if "api_key_env" in config:
            require_text(config["api_key_env"], "api_key_env")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config["api_key_env"]):
                raise ValueError("api_key_env must be an environment variable name")
        if "env_file" in config:
            require_text(config["env_file"], "env_file")
            if "api_key_env" not in config:
                raise ValueError("env_file requires api_key_env")
        if config.get("token_field", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("token_field must be max_tokens or max_completion_tokens")
        if type(config.get("json_mode", True)) is not bool:
            raise ValueError("json_mode must be a boolean")
        if "temperature" in config and (type(config["temperature"]) not in (int, float) or
                                         not 0 <= config["temperature"] < float("inf")):
            raise ValueError("temperature must be a finite nonnegative number")
        extra = config.get("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("Chat extra must be a table/object")
        if set(extra) & {"messages", "model", "stream", "response_format", "max_tokens", "max_completion_tokens"}:
            raise ValueError("Chat extra cannot override routing, format, or token fields")
        self.config = config
        base = require_text(config.get("base_url"), "Chat base_url").rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or any(char.isspace() for char in base)):
            raise ValueError("Chat base_url must be an HTTP(S) URL without credentials, query, fragment, or whitespace")
        # Accessing port validates malformed/non-numeric and out-of-range values.
        parsed.port
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self._closed = threading.Event()

    def close(self) -> None:
        self._closed.set()

    @staticmethod
    def _credential_value(value: str, name: str) -> str:
        # urllib header errors may echo malformed values, so reject them first.
        if not value or any(not 33 <= ord(char) <= 126 for char in value):
            raise BackendError(f"Invalid credential characters for {name}")
        return value

    def credential(self, cwd: Path) -> str:
        name = self.config.get("api_key_env")
        if not name:
            return ""
        if os.environ.get(name):
            return self._credential_value(os.environ[name], name)
        if "env_file" in self.config:
            path = cwd / self.config["env_file"]
            if not path.is_file():
                raise BackendError(f"Missing credential file: {self.config['env_file']}")
            values = []
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)", line)
                if match:
                    try:
                        tokens = shlex.split(match[1], comments=True, posix=True)
                    except ValueError:
                        raise BackendError(f"Invalid credential assignment for {name}") from None
                    if len(tokens) != 1 or not tokens[0]:
                        raise BackendError(f"Expected one nonempty credential value for {name}")
                    values.append(tokens[0])
            if len(values) == 1:
                return self._credential_value(values[0], name)
            if len(values) > 1:
                raise BackendError(f"Duplicate credential assignment for {name}")
        raise BackendError(f"Missing credential environment variable: {name}")

    def complete(self, prompt: str, schema: dict, folder: Path, cwd: Path) -> dict:
        if self._closed.is_set():
            raise BackendError("Chat backend is closed; no further calls can start")
        config = self.config
        payload = {"model": config["model"], "messages": [
            {"role": "user", "content": prompt + "\n\nRequired JSON Schema:\n" + json.dumps(schema)}], "stream": False}
        token_field = config.get("token_field", "max_tokens")
        if token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Invalid token_field")
        payload[token_field] = config.get("max_tokens", 8192)
        if "temperature" in config:
            payload["temperature"] = config["temperature"]
        if config.get("json_mode", True):
            payload["response_format"] = {"type": "json_object"}
        extra = config.get("extra", {})
        if set(extra) & {"messages", "model", "stream", "response_format", "max_tokens", "max_completion_tokens"}:
            raise ValueError("Chat extra cannot override routing, format, or token fields")
        payload.update(extra)
        key_env = config.get("api_key_env")
        key = self.credential(cwd)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        save_json(folder / "http_request.json", {"url": self.url, "body": payload, "api_key_env": key_env,
                                                "env_file": config.get("env_file")})
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers=headers)
        # Local model servers should not be sent through system HTTP proxies.
        handlers = [urllib.request.ProxyHandler({})] if urllib.parse.urlsplit(self.url).hostname in {
            "127.0.0.1", "localhost", "::1"
        } else []
        opener = urllib.request.build_opener(*handlers)
        if self._closed.is_set():
            raise BackendError("Chat backend is closed; no further calls can start")
        try:
            with opener.open(request, timeout=config.get("timeout", 120)) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            (folder / "http_error.txt").write_text(raw.replace(key, "[REDACTED]") if key else raw)
            raise BackendError(f"HTTP {exc.code}; see http_error.txt") from exc
        raw = raw.replace(key, "[REDACTED]") if key else raw
        (folder / "http_response.json").write_text(raw)
        body = json.loads(raw)
        save_json(folder / "usage.json", body.get("usage"))
        choice = body["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") != "stop" or message.get("refusal") or message.get("tool_calls"):
            raise BackendError("Chat response is incomplete, refused, filtered, or contains tool calls")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise BackendError("Chat response has no final text content")
        if content.strip().startswith("```") and content.strip().endswith("```"):
            content = "\n".join(content.strip().splitlines()[1:-1])
        return json.loads(content)


def make_backend(config: dict):
    if config.get("kind") == "codex":
        return CodexBackend(config)
    if config.get("kind") == "chat":
        return ChatBackend(config)
    raise ValueError("Backend kind must be codex or chat")
