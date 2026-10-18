"""Render editable prompts and checkpoint individual model calls."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import time

from .core import canonical, digest, save_json, validate_json


class Context:
    def __init__(self, source, backend, backend_name, folder, prompt_paths, params, *, retry_failed=False,
                 assert_state=None, prompt_seed=True):
        self.source = source
        self.backend = backend
        self.backend_name = backend_name
        self.folder = folder
        self.prompt_paths = prompt_paths
        self.params = params
        self.completed = {}
        self.call_seeds = {}
        self.retry_failed = retry_failed
        self.assert_state = assert_state
        self.prompt_seed = prompt_seed

    def prompt(self, name):
        path = self.prompt_paths.get(name, Path(__file__).parent / "prompts" / f"{name}.md")
        return Path(path).read_text(encoding="utf-8")

    def generation_prompt(self, method, source, params):
        text = (self.prompt("system") + "\n\nMETHOD:\n" + self.prompt(method) +
                "\n\nPARAMETERS (count is an upper bound):\n" + canonical(params) +
                "\n\nSOURCE MATERIAL:\n" + source.context_text() +
                "\n\nReturn only the requested JSON object. If unsuitable, return "
                '{"candidates": [], "empty_reason": "specific reason"}. '
                "Each candidate needs instruction, answer, evidence, basis, uncertainty. "
                "All fields are required; uncertainty may be empty. Evidence is an array of "
                "{path, start_line, end_line}. For direct text without paths use an empty array. "
                "Do not include the source excerpt in instruction: the runner attaches it for "
                "reading, reflection, and exploration. For proof tasks, make instruction self-contained.")
        if self.folder is None and self.prompt_seed:
            text += "\n\nRandom seed: <generated-and-recorded-for-each-new-call>"
        return text

    @staticmethod
    def seeded_prompt(prompt, seed):
        return prompt + f"\n\nRandom seed: {seed}" if seed is not None else prompt

    def check_seed_exclusion(self, result):
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
            return
        seeds = [seed for seed in self.call_seeds.values() if seed is not None]
        for candidate in result["candidates"]:
            if not isinstance(candidate, dict):
                continue
            if any(seed in candidate.get(field, "") for seed in seeds for field in ("instruction", "answer")
                   if isinstance(candidate.get(field), str)):
                raise ValueError("Student content contains a prompt seed; inspect the response before retrying")

    def call(self, stage, prompt, schema, validator=None):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stage):
            raise ValueError("Stage names must be lower-case identifiers")
        if self.assert_state:
            self.assert_state()
        self.source.assert_unchanged()
        # Request identity is stable across recovery; each real attempt has its own seed.
        signature = digest({"backend": self.backend_name, "prompt": prompt, "schema": schema,
                            "prompt_seed": self.prompt_seed})
        stage_dir = self.folder / "steps" / stage
        checkpoint = stage_dir / "checkpoint.json"
        if checkpoint.exists():
            stored = json.loads(checkpoint.read_text())
            if stored["signature"] != signature:
                raise ValueError("A checkpoint request changed; choose a new run directory")
            seed = stored.get("random_seed")
            request_signature = digest({"backend": self.backend_name,
                                        "prompt": self.seeded_prompt(prompt, seed), "schema": schema})
            if stored.get("request_signature") != request_signature:
                raise ValueError("Checkpoint seed/request identity is inconsistent; inspect the saved call")
            self.call_seeds[stage] = seed
            result = stored["result"]
        else:
            stage_dir.mkdir(parents=True, exist_ok=True)
            attempts = sorted(stage_dir.glob("attempt-*"))
            if attempts and not self.retry_failed:
                raise ValueError(f"Unfinished call {stage}; inspect it and use --retry-failed to try again")
            numbers = [int(p.name[8:]) for p in attempts if p.name[8:].isdigit()]
            attempt = stage_dir / f"attempt-{max(numbers, default=0) + 1:03d}"
            attempt.mkdir()
            seed = secrets.token_hex(16) if self.prompt_seed else None
            self.call_seeds[stage] = seed
            actual_prompt = self.seeded_prompt(prompt, seed)
            request_signature = digest({"backend": self.backend_name, "prompt": actual_prompt, "schema": schema})
            save_json(attempt / "request.json", {"backend": self.backend_name, "prompt": actual_prompt,
                      "schema": schema, "random_seed": seed, "signature": request_signature, "base_signature": signature})
            started = time.monotonic()
            trace = {"format_version": 2, "source_id": self.source.id, "stage": stage,
                     "purpose": "review" if stage.startswith("review_") else "generation",
                     "backend": self.backend_name, "backend_kind": getattr(self.backend, "kind", "custom"),
                     "signature": request_signature, "base_signature": signature, "random_seed": seed,
                     "attempt": attempt.name, "status": "started",
                     "started_at_utc": datetime.now(timezone.utc).isoformat()}
            save_json(attempt / "trace.json", trace)
            print(f"CALL {self.source.id} {self.folder.name} {stage}", flush=True)
            try:
                result = self.backend.complete(actual_prompt, schema, attempt, self.source.root)
                save_json(attempt / "response.json", result)
                validate_json(result, schema)
                self.check_seed_exclusion(result)
                if validator:
                    validator(result)
                self.source.assert_unchanged()
                if self.assert_state:
                    self.assert_state()
                save_json(checkpoint, {"signature": signature, "request_signature": request_signature,
                                      "random_seed": seed, "result": result, "attempt": attempt.name})
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
        self.check_seed_exclusion(result)
        if validator:
            validator(result)
        self.completed[stage] = result
        return result
