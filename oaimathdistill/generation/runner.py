"""Configurable jobs, fresh model review, resumability, and training export."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tomllib

from .backends import make_backend
from .context import Context
from .core import (REVIEW_SCHEMA, canonical, check_keys, digest, file_hash, positive, require_text,
                   save_json, text_hash, validate_result, write_jsonl)
from .methods import load_methods
from .sources import SourceLoader


def text_list(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    for item in value:
        require_text(item, name)
    return value


class Plan:
    """Resolved configuration; constructing this object makes no model calls or writes."""

    def __init__(self, path: Path, output: Path | None = None):
        self.path = path.resolve()
        config_text = self.path.read_bytes().decode("utf-8")
        self.config_hash = text_hash(config_text)
        self.config = tomllib.loads(config_text)
        config = self.config
        check_keys(config, {"project_root", "source_root", "run", "backends", "generation", "review",
                            "sources", "methods", "method_files", "prompts"}, "configuration")
        try:
            canonical(config)
        except (ValueError, TypeError) as exc:
            raise ValueError("Configuration must contain finite JSON-compatible values; TOML dates are unsupported") from exc
        for name in ("project_root", "source_root"):
            if name in config:
                require_text(config[name], name)
        check_keys(config.get("run", {}), {"output", "concurrency"}, "run")
        if "output" in config.get("run", {}):
            require_text(config["run"]["output"], "run.output")
        for name in ("generation", "review"):
            check_keys(config.get(name, {}), {"backend"}, name)
            if "backend" in config.get(name, {}):
                require_text(config[name]["backend"], f"{name}.backend")
        for name in ("sources", "methods"):
            if not isinstance(config.get(name, []), list):
                raise ValueError(f"{name} must be an array of tables")
        self.project = (self.path.parent / config.get("project_root", ".")).resolve()
        self.source_root = (self.project / config.get("source_root", "math")).resolve()
        self.output = (self.project / (output or config.get("run", {}).get("output", "runs/generation"))).resolve()
        if self.output.is_relative_to(self.source_root) or self.source_root.is_relative_to(self.output):
            raise ValueError("Output must be outside, and not an ancestor of, the upstream tree")
        self.concurrency = positive(config.get("run", {}).get("concurrency", 1), "concurrency", integer=True)
        plugin_paths = [(self.project / p).resolve() for p in text_list(config.get("method_files", []), "method_files")]
        self.methods = load_methods(plugin_paths)
        if not isinstance(config.get("prompts", {}), dict):
            raise ValueError("prompts must be a table")
        for value in config.get("prompts", {}).values():
            require_text(value, "prompt path")
        self.prompts = {name: (self.project / p).resolve() for name, p in config.get("prompts", {}).items()}
        for name, path in self.prompts.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not path.is_file():
                raise ValueError(f"Invalid prompt override: {name}")
        self.backends = config.get("backends", {})
        if not isinstance(self.backends, dict):
            raise ValueError("backends must be a table")
        self.generator = config.get("generation", {}).get("backend", "codex")
        self.reviewer = config.get("review", {}).get("backend", self.generator)
        if self.generator not in self.backends or self.reviewer not in self.backends:
            raise ValueError("Generation and review must reference configured backends")
        for value in self.backends.values():
            if not isinstance(value, dict):
                raise ValueError("Each backend must be a table")
            if value.get("kind") not in {"codex", "chat"}:
                raise ValueError("Backend kind must be codex or chat")
            if any(key in value for key in ("api_key", "token", "password")):
                raise ValueError("Use api_key_env rather than inline credentials")
            # Adapter construction validates configuration without making a request.
            make_backend(value).close()
        loader = SourceLoader(self.project, self.source_root)
        self.sources = {}
        for spec in config.get("sources", []):
            if not isinstance(spec, dict):
                raise ValueError("Each source must be a table")
            if not isinstance(spec.get("id"), str) or not spec["id"].strip() or spec["id"] in self.sources:
                raise ValueError("Sources need unique nonempty string ids")
            self.sources[spec["id"]] = loader.load(spec)
        self.jobs = []
        ids = set()
        for number, spec in enumerate(config.get("methods", [])):
            check_keys(spec, {"name", "id", "enabled", "sources", "params"}, "method")
            if type(spec.get("enabled", True)) is not bool:
                raise ValueError("method.enabled must be a boolean")
            if not spec.get("enabled", True):
                continue
            method = require_text(spec.get("name"), "method.name")
            if method not in self.methods:
                raise ValueError(f"Unknown method: {method}")
            if not isinstance(spec.get("params", {}), dict):
                raise ValueError("method.params must be a table")
            if "id" in spec:
                require_text(spec["id"], "method.id")
            params = {"count": 1, **spec.get("params", {})}
            if method == "reading":
                params.setdefault("form", "qa")
            positive(params["count"], "candidate count", integer=True)
            module = self.methods[method]
            if hasattr(module, "validate_params"):
                module.validate_params(params)
            for source_id in text_list(spec.get("sources", list(self.sources)), "method.sources"):
                if source_id not in self.sources:
                    raise ValueError(f"Unknown source: {source_id}")
                source = self.sources[source_id]
                if source.mode == "agent" and any(self.backends[b]["kind"] != "codex" for b in (self.generator, self.reviewer)):
                    raise ValueError("Agent inputs require file-capable generation and review backends")
                key = spec.get("id", f"{number}-{method}")
                job_id = method + "-" + digest({"key": key, "source": source_id, "params": params})[:16]
                if job_id in ids:
                    raise ValueError("Duplicate job; use distinct method instance ids")
                ids.add(job_id)
                self.jobs.append({"id": job_id, "method": method, "source_id": source_id, "params": params})
        if not self.jobs:
            raise ValueError("The configuration selects no jobs")
        implementation = {str(p.relative_to(Path(__file__).parent)): file_hash(p)
                          for p in sorted(Path(__file__).parent.rglob("*")) if p.suffix in {".py", ".md"}}
        dependencies = [Path(__file__).parent.parent / "extract_sources.py", *plugin_paths, *self.prompts.values()]
        implementation.update({str(p): file_hash(p) for p in dependencies})
        identity_config = {**config, "run": {k: v for k, v in config.get("run", {}).items() if k != "output"}}
        self.identity = {"format_version": 1, "config": identity_config, "jobs": self.jobs,
                         "sources": {k: s.snapshot() for k, s in self.sources.items()}, "implementation": implementation}
        self.fingerprint = digest(self.identity)

    def assert_implementation_unchanged(self):
        if file_hash(self.path) != self.config_hash:
            raise ValueError("Configuration changed after loading; reload it and choose a new run if needed")
        for key, expected in self.identity["implementation"].items():
            path = Path(key) if Path(key).is_absolute() else Path(__file__).parent / key
            if file_hash(path) != expected:
                raise ValueError(f"Implementation or prompt changed during the run: {path}; use a new run directory")

    def output_status(self):
        if not self.output.exists():
            return "new"
        if (self.output / ".run.lock").exists():
            return "locked"
        manifest = self.output / "run.json"
        if manifest.is_file():
            try:
                fingerprint = json.loads(manifest.read_text())["fingerprint"]
            except (ValueError, KeyError, TypeError):
                return "invalid_manifest"
            return "resumable" if fingerprint == self.fingerprint else "fingerprint_mismatch"
        return "occupied" if any(self.output.iterdir()) else "new"

    def preview(self):
        result = []
        for job in self.jobs:
            source, module = self.sources[job["source_id"]], self.methods[job["method"]]
            context = Context(source, None, self.generator, None, self.prompts, job["params"])
            request = module.preview(source, context, job["params"]) if hasattr(module, "preview") else {
                "note": "Custom method has no preview(); generate() has not been executed"}
            result.append({**job, "input_mode": source.mode, "backend": self.generator, "request": request})
        known_methods = {"local_proof", "proof_completion", "proof_repair", "reading", "reflection", "exploration"}
        known_budget = all(job["method"] in known_methods for job in self.jobs)
        overview = {"jobs": len(self.jobs), "candidate_limit": sum(j["params"]["count"] for j in self.jobs),
                    "output_status": self.output_status(),
                    "concurrency": self.concurrency, "generation_backend": self.generator, "review_backend": self.reviewer,
                    "maximum_calls_before_retries": sum(1 + (j["method"] == "proof_repair") + j["params"]["count"]
                                                         for j in self.jobs) if known_budget else None,
                    "backends": {name: {"kind": self.backends[name]["kind"],
                                       "model": self.backends[name].get("model", "inherited"),
                                       "timeout": self.backends[name].get("timeout", 600 if self.backends[name]["kind"] == "codex" else 120)}
                                 for name in sorted({self.generator, self.reviewer})}}
        return {"fingerprint": self.fingerprint, "output": str(self.output), "overview": overview, "jobs": result}


def public_source(source):
    snapshot = source.snapshot()
    snapshot["files"] = {k: {f: v for f, v in record.items() if f != "absolute_path"}
                         for k, record in snapshot["files"].items()}
    return snapshot


def student_messages(source, method, item):
    instruction = item["instruction"]
    if method in {"reading", "reflection", "exploration"}:
        instruction = "Source excerpt:\n\n" + source.context_text(item["evidence"]) + "\n\nTask:\n" + instruction
    return [{"role": "user", "content": instruction}, {"role": "assistant", "content": item["answer"]}]


def process_job(plan, job, backends, *, retry_failed=False):
    source = plan.sources[job["source_id"]]
    module = plan.methods[job["method"]]
    folder = plan.output / "jobs" / job["id"]
    final = folder / "result.json"
    if final.exists():
        record = json.loads(final.read_text())
        if record["status"] != "failed" or not retry_failed:
            print(f"RESUME {job['id']} {record['status']}", flush=True)
            return record
    context = Context(source, backends[plan.generator], plan.generator, folder, plan.prompts,
                      job["params"], retry_failed=retry_failed, assert_state=plan.assert_implementation_unchanged)
    record = {**job, "source": public_source(source), "candidates": []}
    try:
        source.assert_unchanged()
        generated = module.generate(source, context, job["params"])
        validate_result(generated, job["params"]["count"])
        save_json(folder / "generated.json", generated)
        for index, item in enumerate(generated["candidates"]):
            evidence = source.check_evidence(item["evidence"])
            candidate = {"id": f"{job['id']}#{index}", "messages": student_messages(source, job["method"], item),
                         "construction": item, "program_checks": {"schema": "passed", "source_locations": "passed",
                         "source_hashes": "passed", "mathematical_proof_check": "not_performed"},
                         "meta": {"method": job["method"], "params": job["params"], "source": public_source(source),
                                  "evidence": [{k: v for k, v in ref.items() if k != "text"} for ref in evidence],
                                  "run_fingerprint": plan.fingerprint,
                                  "generation_backend": plan.generator, "review_backend": plan.reviewer}}
            record["candidates"].append(candidate)
            review_context = Context(source, backends[plan.reviewer], plan.reviewer, folder, plan.prompts,
                                     job["params"], retry_failed=retry_failed, assert_state=plan.assert_implementation_unchanged)
            review_prompt = (context.prompt("system") + "\n\n" + context.prompt("review") +
                             "\n\nMETHOD: " + job["method"] + "\nPARAMETERS: " + canonical(job["params"]) +
                             "\n\nSOURCE:\n" + source.context_text() +
                             "\n\nCANDIDATE:\n" + canonical(candidate) +
                             "\n\nPRIVATE CONSTRUCTION STEPS:\n" + canonical(context.completed))

            def validate_review(value):
                if not value["reasons"] or any(not reason.strip() for reason in value["reasons"]) or not value["evidence_scope"].strip():
                    raise ValueError("A review needs concrete reasons and an evidence scope")

            candidate["review"] = review_context.call(f"review_{index}", review_prompt, REVIEW_SCHEMA, validate_review)
        source.assert_unchanged()
        record["status"] = "completed" if generated["candidates"] else "empty"
        record["empty_reason"] = generated["empty_reason"]
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
    save_json(final, record)
    print(f"DONE {job['id']} {record['status']}", flush=True)
    return record


def trace_index(output: Path, manifest: dict) -> list[dict]:
    """Index every application call attempt without rewriting its original artifacts."""
    traces = []
    for job in manifest["identity"]["jobs"]:
        steps = output / "jobs" / job["id"] / "steps"
        for stage_dir in sorted(steps.glob("*")):
            checkpoint_file = stage_dir / "checkpoint.json"
            checkpoint = json.loads(checkpoint_file.read_text()) if checkpoint_file.is_file() else {}
            for attempt in sorted(stage_dir.glob("attempt-*")):
                trace_file, request_file = attempt / "trace.json", attempt / "request.json"
                trace = json.loads(trace_file.read_text()) if trace_file.is_file() else {}
                request = json.loads(request_file.read_text()) if request_file.is_file() else {}
                checkpointed = checkpoint.get("attempt") == attempt.name
                # Older runs have raw artifacts but no lifecycle metadata. Do not invent timings.
                status = trace.get("status", "completed" if checkpointed else
                                   "failed" if (attempt / "error.json").is_file() else "unrecorded")
                traces.append({"job_id": job["id"], "source_id": job["source_id"], "method": job["method"],
                               "stage": stage_dir.name,
                               "purpose": "review" if stage_dir.name.startswith("review_") else "generation",
                               "backend": request.get("backend"), "backend_kind": trace.get("backend_kind"),
                               "attempt": attempt.name, "status": status, "status_scope": "application_call",
                               "checkpointed": checkpointed, "run_fingerprint": manifest["fingerprint"],
                               "started_at_utc": trace.get("started_at_utc"),
                               "finished_at_utc": trace.get("finished_at_utc"),
                               "elapsed_seconds": trace.get("elapsed_seconds"),
                               "checkpoint": str(checkpoint_file.relative_to(output)) if checkpointed else None,
                               "files": {p.name: str(p.relative_to(output)) for p in sorted(attempt.iterdir()) if p.is_file()}})
    return traces


@contextmanager
def run_lock(output: Path):
    lock = output / ".run.lock"
    try:
        descriptor = lock.open("x")
    except FileExistsError as exc:
        raise ValueError("Run directory is locked; stop its owner before removing a stale .run.lock") from exc
    try:
        with descriptor:
            import os
            descriptor.write(str(os.getpid()) + "\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def export_run(output: Path) -> dict:
    with run_lock(output):
        return _export_run(output)


def _export_run(output: Path) -> dict:
    manifest = json.loads((output / "run.json").read_text())
    candidates, reviews, training, outcomes = [], [], [], []
    duplicates = set()
    for job in manifest["identity"]["jobs"]:
        result_path = output / "jobs" / job["id"] / "result.json"
        record = json.loads(result_path.read_text()) if result_path.exists() else {**job, "status": "pending", "candidates": []}
        outcomes.append({k: v for k, v in record.items() if k != "candidates"})
        for candidate in record["candidates"]:
            candidates.append(candidate)
            if "review" not in candidate:
                continue
            reviews.append({"id": candidate["id"], **candidate["review"]})
            checks = candidate["program_checks"]
            key = digest(candidate["messages"])
            if (record["status"] == "completed" and candidate["review"]["status"] == "passed" and
                all(checks.get(k) == "passed" for k in ("schema", "source_locations", "source_hashes")) and key not in duplicates):
                duplicates.add(key)
                training.append({"id": candidate["id"], "messages": candidate["messages"], "meta": {
                    **candidate["meta"], "review": candidate["review"], "program_checks": checks,
                    "evidence_label": "model_review_passed"}})
    traces = trace_index(output, manifest)
    for name, records in (("candidates", candidates), ("reviews", reviews), ("train", training), ("outcomes", outcomes),
                          ("traces", traces)):
        write_jsonl(output / f"{name}.jsonl", records)
    summary = {"jobs": len(outcomes), "job_statuses": dict(Counter(r["status"] for r in outcomes)),
               "candidates": len(candidates), "review_statuses": dict(Counter(r["status"] for r in reviews)),
               "exported": len(training), "run_fingerprint": manifest["fingerprint"],
               "call_attempts": len(traces), "call_statuses": dict(Counter(r["status"] for r in traces))}
    save_json(output / "summary.json", summary)
    lines = ["# Generation run report", "", "## Observed execution", "", "```json", json.dumps(summary, indent=2), "```", "",
             "| Source | Method | Form | Outcome | Candidates |", "| --- | --- | --- | --- | --- |"]
    for outcome in outcomes:
        count = sum(c["id"].startswith(outcome["id"] + "#") for c in candidates)
        lines.append(f"| {outcome['source_id']} | {outcome['method']} | {outcome['params'].get('form', '')} | {outcome['status']} | {count} |")
    lines += ["", "## Evidence boundary", "", "Program checks cover response structure, source locations, and source hashes. "
              "Mathematical content is assessed by a separate model request, not an independent proof certificate. "
              "No model training or downstream improvement is demonstrated by this run."]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def run(plan: Plan, *, retry_failed=False):
    output = plan.output
    plan.assert_implementation_unchanged()
    output.mkdir(parents=True, exist_ok=True)
    with run_lock(output):
        return _run_locked(plan, retry_failed=retry_failed)


def _run_locked(plan: Plan, *, retry_failed=False):
    output = plan.output
    backends = {}
    try:
        manifest_file = output / "run.json"
        if manifest_file.exists():
            if json.loads(manifest_file.read_text())["fingerprint"] != plan.fingerprint:
                raise ValueError("Source, method, prompt, or configuration changed; use a new output directory")
        elif any(p.name != ".run.lock" for p in output.iterdir()):
            raise ValueError("A new run needs an empty output directory")
        else:
            versions = {}
            for name in sorted({plan.generator, plan.reviewer}):
                settings = plan.backends[name]
                if settings["kind"] == "codex":
                    command = settings.get("command", ["codex"])
                    try:
                        probe = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=10)
                        versions[name] = probe.stdout.strip() if probe.returncode == 0 else "unavailable"
                    except (OSError, subprocess.TimeoutExpired):
                        versions[name] = "unavailable"
            save_json(manifest_file, {"fingerprint": plan.fingerprint, "identity": plan.identity, "backend_versions": versions,
                                      "created_at_utc": datetime.now(timezone.utc).isoformat()})
        for source in plan.sources.values():
            source.assert_unchanged()
        backends = {name: make_backend(plan.backends[name]) for name in sorted({plan.generator, plan.reviewer})}
        pool = ThreadPoolExecutor(max_workers=plan.concurrency)
        futures = []
        try:
            futures = [pool.submit(process_job, plan, job, backends, retry_failed=retry_failed) for job in plan.jobs]
            for future in as_completed(futures):
                future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            for backend in backends.values():
                backend.close()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        for source in plan.sources.values():
            source.assert_unchanged()
        plan.assert_implementation_unchanged()
        return _export_run(output)
    finally:
        for backend in backends.values():
            backend.close()
