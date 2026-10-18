"""Render editable prompts and checkpoint individual model calls."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .core import canonical, digest, save_json, validate_json


class Context:
    def __init__(self, source, backend, backend_name, folder, prompt_paths, params, *, retry_failed=False, assert_state=None):
        self.source = source
        self.backend = backend
        self.backend_name = backend_name
        self.folder = folder
        self.prompt_paths = prompt_paths
        self.params = params
        self.completed = {}
        self.retry_failed = retry_failed
        self.assert_state = assert_state

    def prompt(self, name):
        path = self.prompt_paths.get(name, Path(__file__).parent / "prompts" / f"{name}.md")
        return Path(path).read_text(encoding="utf-8")

    def generation_prompt(self, method, source, params):
        return (self.prompt("system") + "\n\nMETHOD:\n" + self.prompt(method) +
                "\n\nPARAMETERS (count is an upper bound):\n" + canonical(params) +
                "\n\nSOURCE MATERIAL:\n" + source.context_text() +
                "\n\nReturn only the requested JSON object. If unsuitable, return "
                '{"candidates": [], "empty_reason": "specific reason"}. '
                "Each candidate needs instruction, answer, evidence, basis, uncertainty. "
                "All fields are required; uncertainty may be empty. Evidence is an array of "
                "{path, start_line, end_line}. For direct text without paths use an empty array. "
                "Do not include the source excerpt in instruction: the runner attaches it for "
                "reading, reflection, and exploration. For proof tasks, make instruction self-contained.")

    def call(self, stage, prompt, schema, validator=None):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stage):
            raise ValueError("Stage names must be lower-case identifiers")
        if self.assert_state:
            self.assert_state()
        self.source.assert_unchanged()
        signature = digest({"backend": self.backend_name, "prompt": prompt, "schema": schema})
        stage_dir = self.folder / "steps" / stage
        checkpoint = stage_dir / "checkpoint.json"
        if checkpoint.exists():
            stored = json.loads(checkpoint.read_text())
            if stored["signature"] != signature:
                raise ValueError("A checkpoint request changed; choose a new run directory")
            result = stored["result"]
        else:
            stage_dir.mkdir(parents=True, exist_ok=True)
            attempts = sorted(stage_dir.glob("attempt-*"))
            if attempts and not self.retry_failed:
                raise ValueError(f"Unfinished call {stage}; inspect it and use --retry-failed to try again")
            numbers = [int(p.name[8:]) for p in attempts if p.name[8:].isdigit()]
            attempt = stage_dir / f"attempt-{max(numbers, default=0) + 1:03d}"
            attempt.mkdir()
            save_json(attempt / "request.json", {"backend": self.backend_name, "prompt": prompt, "schema": schema})
            started = time.monotonic()
            trace = {"format_version": 1, "source_id": self.source.id, "stage": stage,
                     "purpose": "review" if stage.startswith("review_") else "generation",
                     "backend": self.backend_name, "backend_kind": getattr(self.backend, "kind", "custom"),
                     "signature": signature, "attempt": attempt.name, "status": "started",
                     "started_at_utc": datetime.now(timezone.utc).isoformat()}
            save_json(attempt / "trace.json", trace)
            print(f"CALL {self.source.id} {self.folder.name} {stage}", flush=True)
            try:
                result = self.backend.complete(prompt, schema, attempt, self.source.root)
                save_json(attempt / "response.json", result)
                validate_json(result, schema)
                if validator:
                    validator(result)
                self.source.assert_unchanged()
                if self.assert_state:
                    self.assert_state()
                save_json(checkpoint, {"signature": signature, "result": result, "attempt": attempt.name})
                trace["status"] = "completed"
            except BaseException as exc:
                trace["status"] = "failed" if isinstance(exc, Exception) else "interrupted"
                save_json(attempt / "error.json", {"type": type(exc).__name__, "message": str(exc)})
                raise
            finally:
                trace["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                trace["elapsed_seconds"] = time.monotonic() - started
                save_json(attempt / "trace.json", trace)
        validate_json(result, schema)
        if validator:
            validator(result)
        self.completed[stage] = result
        return result
