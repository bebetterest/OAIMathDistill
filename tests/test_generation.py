"""Offline integration checks for source fidelity, methods, and run state."""

from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oaimathdistill.generation.backends import BackendError, ChatBackend, CodexBackend
from oaimathdistill.generation.context import Context
from oaimathdistill.generation.core import RESULT_SCHEMA, REVIEW_SCHEMA, file_hash, text_hash, validate_result
from oaimathdistill.generation.methods import load_methods
from oaimathdistill.generation.runner import Plan, export_run, run, trace_index
from oaimathdistill.generation.sources import SourceLoader


def candidate(evidence=None):
    return {"instruction": "Explain the role of x > 0.", "answer": "Positivity permits division by x.",
            "evidence": evidence or [], "basis": "The supplied deduction divides by x.", "uncertainty": ""}


@contextmanager
def http_fixture(body, code=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), payload))
            self.send_response(code)
            self.end_headers()
            reply = body(payload) if callable(body) else body
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class FakeBackend:
    supports_files = True

    def __init__(self, *, empty=False, review="passed", fail_stage=None):
        self.calls = []
        self.empty, self.review, self.fail_stage = empty, review, fail_stage

    def close(self):
        pass

    def complete(self, prompt, schema, folder, cwd):
        self.calls.append((prompt, schema))
        stage = folder.parent.name
        if stage == self.fail_stage:
            raise BackendError("fixture failure")
        if schema == REVIEW_SCHEMA:
            return {"status": self.review, "reasons": ["Fixture review only."], "evidence_scope": "Not mathematical validation."}
        if self.empty:
            return {"candidates": [], "empty_reason": "No usable argument in this definition-only fragment."}
        evidence = [{"path": "math/preprints/selected/main.tex", "start_line": 1, "end_line": 2}]
        item = candidate(evidence)
        if '"form":"explanation"' in prompt:
            item["instruction"] = "Give a connected explanation of division by a positive x."
        return {"candidates": [item], "empty_reason": None}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "math"
        self.paper = source / "preprints/selected/main.tex"
        self.paper.parent.mkdir(parents=True)
        self.paper.write_bytes(b"Assume x > 0.\r\nThen x/x = 1.\r\n")
        (source / "CONTENTS.md").write_text("**001. Selected.**\n[Paper](preprints/selected/paper.pdf)\n")
        (source / "README.md").write_text("Fixture\n")
        self.tracked = ["preprints/selected/main.tex"]

        def git(root, *args):
            return {("ls-files", "-z"): "\0".join(self.tracked), ("rev-parse", "HEAD"): "fixture-revision",
                    ("status", "--porcelain"): ""}[args]

        self.git = patch("oaimathdistill.extract_sources.git_output", side_effect=git)
        self.git.start()
        self.addCleanup(self.git.stop)

    def config(self, methods=None, *, two_sources=True):
        path = self.root / "config.toml"
        text = '''project_root = "."
source_root = "math"
[run]
output = "runs/test"
concurrency = 1
[backends.fixture]
kind = "codex"
command = ["codex"]
timeout = 10
[generation]
backend = "fixture"
[[sources]]
id = "fragment"
mode = "fragment"
path = "math/preprints/selected/main.tex"
ranges = [[1, 2]]
'''
        if two_sources:
            text += '''[[sources]]
id = "agent"
mode = "agent"
path = "math/preprints/selected/main.tex"
'''
        for number, method in enumerate(methods or list(load_methods())):
            form = "explanation" if method == "reading" and number > 5 else "qa"
            text += f'''[[methods]]
name = "{method}"
id = "instance_{number}"
params = {{count = 1, form = "{form}"}}
'''
        path.write_text(text)
        return path

    def execute(self, plan, backend, *, retry_failed=False):
        with patch("oaimathdistill.generation.runner.make_backend", return_value=backend), patch(
            "oaimathdistill.generation.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "fixture CLI", "")
        ):
            return run(plan, retry_failed=retry_failed)

    def api_config(self, url, *, literal=False):
        config = self.config(["reading"], two_sources=False)
        text = config.read_text().replace('[backends.fixture]\nkind = "codex"\ncommand = ["codex"]',
            f'[backends.fixture]\nkind = "chat"\nbase_url = "{url}"\nmodel = "generation-fixture"')
        if literal:
            text = text.replace('path = "math/preprints/selected/main.tex"\nranges = [[1, 2]]',
                                'text = "Assume x > 0. Then x/x = 1."')
        config.write_text(text + f'''\n[backends.review_api]
kind = "chat"
base_url = "{url}"
model = "review-fixture"
[review]
backend = "review_api"
''')
        return config


class GenerationTests(Fixture):
    def test_all_methods_both_inputs_and_reading_forms(self):
        config = self.config([*load_methods(), "reading"])
        before = file_hash(self.paper)
        plan, backend = Plan(config), FakeBackend()
        summary = self.execute(plan, backend)
        self.assertEqual(summary["jobs"], 14)
        self.assertEqual(summary["candidates"], 14)
        self.assertEqual(summary["job_statuses"], {"completed": 14})
        rows = [json.loads(line) for line in (plan.output / "train.jsonl").read_text().splitlines()]
        self.assertTrue(any(row["meta"]["params"]["form"] == "explanation" for row in rows))
        self.assertEqual(before, file_hash(self.paper))
        for row in rows:
            self.assertEqual(row["meta"]["source"]["provenance"]["family_ids"], ["001"])
            self.assertEqual(row["meta"]["evidence_label"], "model_review_passed")
            self.assertNotIn("absolute_path", json.dumps(row["meta"]["source"]))
            if row["meta"]["method"] in {"reading", "reflection", "exploration"}:
                self.assertIn("Source excerpt:", row["messages"][0]["content"])
            self.assertEqual(row["messages"][1]["content"], candidate()["answer"])
        old_calls = len(backend.calls)
        self.execute(plan, backend)
        self.assertEqual(len(backend.calls), old_calls)
        self.assertEqual(export_run(plan.output), summary)

    def test_empty_is_success_skips_repair_and_review(self):
        plan, backend = Plan(self.config()), FakeBackend(empty=True)
        summary = self.execute(plan, backend)
        self.assertEqual(summary["job_statuses"], {"empty": 12})
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(len(backend.calls), 12)
        self.assertTrue(all(schema != REVIEW_SCHEMA for _, schema in backend.calls))
        self.assertFalse((plan.output / "train.jsonl").read_text())

    def test_repair_resumes_draft_after_later_failure(self):
        plan = Plan(self.config(["proof_repair"], two_sources=False))
        backend = FakeBackend(fail_stage="repair")
        self.assertEqual(self.execute(plan, backend)["job_statuses"], {"failed": 1})
        self.assertEqual(len(backend.calls), 2)
        backend.fail_stage = None
        self.execute(plan, backend)
        self.assertEqual(len(backend.calls), 2)
        summary = self.execute(plan, backend, retry_failed=True)
        self.assertEqual(summary["job_statuses"], {"completed": 1})
        self.assertEqual(len(backend.calls), 4)
        self.assertEqual(len(list(plan.output.glob("jobs/*/steps/draft/attempt-*"))), 1)
        self.assertEqual(len(list(plan.output.glob("jobs/*/steps/repair/attempt-*"))), 2)
        traces = [json.loads(line) for line in (plan.output / "traces.jsonl").read_text().splitlines()]
        self.assertEqual(len(traces), 4)
        self.assertEqual(sum(t["status"] == "failed" for t in traces), 1)
        self.assertEqual(sum(t["checkpointed"] for t in traces), 3)
        self.assertEqual({t["purpose"] for t in traces}, {"generation", "review"})

    def test_review_status_controls_export(self):
        for status in ("rejected", "needs_review"):
            with self.subTest(status=status):
                plan = Plan(self.config(["reading"], two_sources=False), Path(f"runs/{status}"))
                summary = self.execute(plan, FakeBackend(review=status))
                self.assertEqual(summary["exported"], 0)
                self.assertEqual(summary["review_statuses"], {status: 1})

    def test_source_prompt_and_config_changes_require_new_run(self):
        config = self.config(["reading"], two_sources=False)
        plan = Plan(config)
        self.execute(plan, FakeBackend())
        custom = self.root / "reading.md"
        custom.write_text("Custom reading prompt")
        config.write_text(config.read_text() + '\n[prompts]\nreading = "reading.md"\n')
        changed = Plan(config)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.execute(changed, FakeBackend())
        custom.write_text("Updated custom prompt")
        self.assertNotEqual(Plan(config).fingerprint, changed.fingerprint)
        self.paper.write_text("Different source.\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            plan.sources["fragment"].assert_unchanged()

    def test_preview_does_not_call_or_write_and_override_is_used(self):
        config = self.config(["reading"], two_sources=False)
        custom = self.root / "custom.md"
        custom.write_text("Distinctive overridden prompt")
        config.write_text(config.read_text() + '\n[prompts]\nreading = "custom.md"\n')
        plan = Plan(config)
        with patch.object(CodexBackend, "complete", side_effect=AssertionError("Unexpected model call")):
            preview = plan.preview()
        self.assertIn("Distinctive overridden prompt", preview["jobs"][0]["request"]["prompt"])
        self.assertFalse(plan.output.exists())

    def test_source_ranges_and_direct_text_preserve_bytes(self):
        loader = SourceLoader(self.root, self.root / "math")
        source = loader.load({"id": "fragment", "path": str(self.paper), "ranges": [[2, 2]]})
        self.assertEqual(source.blocks[0]["sha256"], text_hash("Then x/x = 1.\r\n"))
        with self.assertRaisesRegex(ValueError, "beyond"):
            source.check_evidence([{"path": "math/preprints/selected/main.tex", "start_line": 1, "end_line": 2}])
        direct = loader.load({"id": "text", "text": "A definition without a proof.\r\n"})
        self.assertEqual(direct.text, "A definition without a proof.\r\n")
        self.assertEqual(direct.check_evidence([]), [])

    def test_default_reading_form_is_recorded_and_run_options_are_fingerprinted(self):
        config = self.config(["reading"], two_sources=False)
        config.write_text(config.read_text().replace('params = {count = 1, form = "qa"}', 'params = {count = 1}'))
        before = Plan(config)
        self.assertEqual(before.jobs[0]["params"]["form"], "qa")
        config.write_text(config.read_text().replace("concurrency = 1", "concurrency = 2"))
        self.assertNotEqual(before.fingerprint, Plan(config).fingerprint)

    def test_preview_rejects_invalid_backend_without_network_or_output(self):
        config = self.config(["reading"], two_sources=False)
        text = config.read_text().replace('kind = "codex"', 'kind = "chat"\nbase_url = "https://models.example.invalid/v1"')
        text = text.replace('command = ["codex"]\n', '')
        config.write_text(text)
        with self.assertRaisesRegex(ValueError, "explicit model"):
            Plan(config)
        self.assertFalse((self.root / "runs").exists())
        config.write_text(text.replace('kind = "chat"', 'kind = "chat"\nmodel = "fixture"'))
        self.assertEqual(len(Plan(config).preview()["jobs"]), 1)

    def test_active_catalogue_and_native_only_boundary(self):
        loader = SourceLoader(self.root, self.root / "math")
        with self.assertRaisesRegex(ValueError, "current-catalogue"):
            loader.load({"id": "bad", "path": "math/README.md"})
        pdf = self.paper.with_suffix(".pdf")
        pdf.write_bytes(b"Never read")
        self.tracked.append(str(pdf.relative_to(self.root / "math")))
        with self.assertRaisesRegex(ValueError, "Select one"):
            loader.load({"id": "pdf", "path": str(pdf)})
        with self.assertRaisesRegex(ValueError, "outside"):
            Plan(self.config(), Path("math/output"))

    def test_external_method_can_use_context_and_empty_contract(self):
        external = self.root / "extra.py"
        external.write_text('''name = "custom"
description = "Custom local task"
def generate(source, context, params):
    return {"candidates": [], "empty_reason": "Custom applicability decision"}
''')
        self.assertIn("custom", load_methods([external]))
        config = self.config(["reading"], two_sources=False)
        text = config.read_text().replace('source_root = "math"', 'source_root = "math"\nmethod_files = ["extra.py"]')
        config.write_text(text.replace('name = "reading"', 'name = "custom"'))
        plan = Plan(config)
        self.assertIn("no preview", plan.preview()["jobs"][0]["request"]["note"])
        backend = FakeBackend()
        self.assertEqual(self.execute(plan, backend)["job_statuses"], {"empty": 1})
        self.assertEqual(backend.calls, [])

    def test_invalid_and_overfull_results_are_not_empty_success(self):
        with self.assertRaisesRegex(ValueError, "empty_reason"):
            validate_result({"candidates": [], "empty_reason": ""}, 1)
        with self.assertRaisesRegex(ValueError, "limit"):
            validate_result({"candidates": [candidate(), candidate()], "empty_reason": None}, 1)
        with self.assertRaises(ValueError):
            validate_result({"candidates": [{**candidate(), "answer": ""}], "empty_reason": None}, 1)

    def test_example_method_attaches_context_for_the_student(self):
        example = Path(__file__).resolve().parents[1] / "oaimathdistill/generation/example_method.py"
        config = self.config(["reading"], two_sources=False)
        config.write_text(config.read_text().replace(
            'source_root = "math"', 'source_root = "math"\nmethod_files = [' + json.dumps(str(example)) + ']'
        ).replace('name = "reading"', 'name = "assumption_analysis"'))
        plan = Plan(config)
        self.execute(plan, FakeBackend())
        rows = [json.loads(line) for line in (plan.output / "train.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertIn("Assume x > 0", rows[0]["messages"][0]["content"])


    def test_fragment_api_generation_and_review_preserve_returned_trajectories(self):
        review = {"status": "passed", "reasons": ["Fixture review only."], "evidence_scope": "Transport test only."}

        def reply(payload):
            reviewer = payload["model"] == "review-fixture"
            result = review if reviewer else {"candidates": [candidate()], "empty_reason": None}
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(result),
                "reasoning_content": "Fixture analysis returned by the service.",
                "reasoning_details": [{"type": "summary", "text": "Fixture summary."}]}}],
                "usage": {"total_tokens": 17}, "provider_trace": {"request_id": "fixture-returned-id"}}

        for literal in (False, True):
            with self.subTest(literal=literal), http_fixture(reply) as (url, requests):
                config = self.api_config(url, literal=literal)
                plan = Plan(config, Path(f"runs/api-{literal}"))
                before = file_hash(self.paper)
                self.assertEqual(run(plan)["exported"], 1)
                self.assertEqual([r[2]["model"] for r in requests], ["generation-fixture", "review-fixture"])
                self.assertIn("Assume x > 0", requests[0][2]["messages"][0]["content"])
                traces = [json.loads(line) for line in (plan.output / "traces.jsonl").read_text().splitlines()]
                self.assertEqual({t["purpose"] for t in traces}, {"generation", "review"})
                self.assertEqual({t["backend"] for t in traces}, {"fixture", "review_api"})
                for trace in traces:
                    self.assertEqual(trace["status"], "completed")
                    self.assertEqual(trace["backend_kind"], "chat")
                    self.assertTrue(trace["checkpointed"])
                    self.assertGreaterEqual(trace["elapsed_seconds"], 0)
                    self.assertLessEqual(trace["started_at_utc"], trace["finished_at_utc"])
                    for reference in trace["files"].values():
                        self.assertTrue((plan.output / reference).is_file())
                    raw = json.loads((plan.output / trace["files"]["http_response.json"]).read_text())
                    self.assertEqual(raw["provider_trace"]["request_id"], "fixture-returned-id")
                    self.assertIn("reasoning_details", raw["choices"][0]["message"])
                    self.assertIn("reasoning_content", raw["choices"][0]["message"])
                training = (plan.output / "train.jsonl").read_text()
                self.assertNotIn("Fixture analysis returned", training)
                self.assertNotIn("Fixture summary", training)
                self.assertEqual(file_hash(self.paper), before)
                index_before = (plan.output / "traces.jsonl").read_bytes()
                self.assertEqual(run(plan)["exported"], 1)
                self.assertEqual(len(requests), 2)
                self.assertEqual((plan.output / "traces.jsonl").read_bytes(), index_before)

    def test_api_review_failure_and_retry_keep_both_attempts(self):
        fail_review = True

        def reply(payload):
            reviewer = payload["model"] == "review-fixture"
            result = ({"status": "passed", "reasons": ["Fixture only."], "evidence_scope": "Transport only."}
                      if reviewer else {"candidates": [candidate()], "empty_reason": None})
            content = "invalid JSON" if reviewer and fail_review else json.dumps(result)
            return {"choices": [{"finish_reason": "stop", "message": {"content": content,
                    "reasoning_content": "Returned fixture analysis, even on invalid output."}}],
                    "usage": {"total_tokens": 7}}

        with http_fixture(reply) as (url, requests):
            plan = Plan(self.api_config(url))
            self.assertEqual(run(plan)["job_statuses"], {"failed": 1})
            review_folder = plan.output / "jobs" / plan.jobs[0]["id"] / "steps/review_0"
            raw_file = review_folder / "attempt-001/http_response.json"
            raw_before = raw_file.read_bytes()
            self.assertEqual(json.loads((review_folder / "attempt-001/trace.json").read_text())["status"], "failed")
            self.assertTrue((review_folder / "attempt-001/usage.json").is_file())
            run(plan)
            self.assertEqual(len(requests), 2)
            fail_review = False
            self.assertEqual(run(plan, retry_failed=True)["exported"], 1)
            self.assertEqual(len(requests), 3)
            self.assertEqual(raw_file.read_bytes(), raw_before)
            traces = [json.loads(line) for line in (plan.output / "traces.jsonl").read_text().splitlines()]
            self.assertEqual(len(traces), 3)
            self.assertEqual(sum(t["status"] == "failed" for t in traces), 1)

    def test_interrupted_call_retains_lifecycle_and_request(self):
        plan = Plan(self.config(["reading"], two_sources=False))
        source = plan.sources["fragment"]
        backend = FakeBackend()

        def interrupt(*args):
            raise KeyboardInterrupt()

        backend.complete = interrupt
        folder = plan.output / "jobs" / plan.jobs[0]["id"]
        context = Context(source, backend, "fixture", folder, {}, {}, retry_failed=False)
        with self.assertRaises(KeyboardInterrupt):
            context.call("generate", "Fixture prompt", RESULT_SCHEMA)
        trace = json.loads((folder / "steps/generate/attempt-001/trace.json").read_text())
        self.assertEqual(trace["status"], "interrupted")
        self.assertIn("finished_at_utc", trace)
        self.assertTrue((folder / "steps/generate/attempt-001/request.json").is_file())
        self.assertFalse((folder / "steps/generate/checkpoint.json").exists())

    def test_legacy_trace_index_links_original_files_without_inventing_timing(self):
        plan = Plan(self.config(["reading"], two_sources=False))
        self.execute(plan, FakeBackend())
        for path in plan.output.glob("jobs/*/steps/*/attempt-*/trace.json"):
            path.unlink()
        manifest = json.loads((plan.output / "run.json").read_text())
        traces = trace_index(plan.output, manifest)
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(t["status"] == "completed" for t in traces))
        self.assertTrue(all(t["started_at_utc"] is None for t in traces))
        self.assertTrue(all("request.json" in t["files"] for t in traces))


class AdapterTests(unittest.TestCase):
    server = staticmethod(http_fixture)

    def test_http_real_local_transport_payload_and_credentials(self):
        result = {"candidates": [], "empty_reason": "Definition only"}
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}], "usage": {"total_tokens": 5}}
        with self.server(body) as (url, requests), tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TEST_GENERATION_KEY": "fixture-secret"}
        ):
            folder = Path(directory)
            backend = ChatBackend({"kind": "chat", "base_url": url, "model": "fixture-model", "api_key_env": "TEST_GENERATION_KEY",
                                   "max_tokens": 100, "token_field": "max_completion_tokens", "extra": {"seed": 1}})
            self.assertEqual(backend.complete("Read the passage", RESULT_SCHEMA, folder, folder), result)
            path, headers, sent = requests[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer fixture-secret")
            self.assertEqual(sent["max_completion_tokens"], 100)
            self.assertNotIn("max_tokens", sent)
            self.assertNotIn("fixture-secret", "".join(p.read_text() for p in folder.iterdir()))

    def test_http_truncation_refusal_and_invalid_json_fail(self):
        for choice in [
            {"finish_reason": "length", "message": {"content": "{}"}},
            {"finish_reason": "stop", "message": {"content": "{}", "refusal": "refused"}},
            {"finish_reason": "stop", "message": {"content": "not json"}},
        ]:
            with self.subTest(choice=choice), self.server({"choices": [choice], "usage": {"total_tokens": 9}}) as (url, _), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                with self.assertRaises((BackendError, json.JSONDecodeError)):
                    ChatBackend({"base_url": url, "model": "fixture"}).complete("test", RESULT_SCHEMA, folder, folder)
                self.assertTrue((folder / "http_response.json").exists())
                self.assertEqual(json.loads((folder / "usage.json").read_text()), {"total_tokens": 9})

    def test_cli_subprocess_flags_output_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fixture.py"
            script.write_text('''import json, os, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
if "timeout" in prompt: time.sleep(10)
if "failure" in prompt: sys.exit(3)
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps({"candidates": [], "empty_reason": "Fixture"}))
print(json.dumps({"type": "fixture.environment", "originator_present": "CODEX_INTERNAL_ORIGINATOR_OVERRIDE" in os.environ}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
''')
            backend = CodexBackend({"command": [sys.executable, "-B", str(script)], "timeout": 2})
            folder = root / "normal"
            folder.mkdir()
            with patch.dict("os.environ", {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
                self.assertEqual(backend.complete("test", RESULT_SCHEMA, folder, root)["candidates"], [])
            events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
            self.assertFalse(events[0]["originator_present"])
            self.assertEqual(json.loads((folder / "invocation.json").read_text())["removed_inherited_environment"],
                             ["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"])
            argv = json.loads((folder / "invocation.json").read_text())["argv"]
            self.assertIn("read-only", argv)
            self.assertIn("--ephemeral", argv)
            self.assertNotIn("--model", argv)
            for prompt, exception in (("failure", BackendError), ("timeout", subprocess.TimeoutExpired)):
                folder = root / prompt
                folder.mkdir()
                started = time.monotonic()
                with self.assertRaises(exception):
                    backend.complete(prompt, RESULT_SCHEMA, folder, root)
                self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(backend._processes)

    def test_closed_cli_backend_cannot_start_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = CodexBackend({"command": ["this-must-not-be-launched"]})
            backend.close()
            with patch("subprocess.Popen", side_effect=AssertionError("Unexpected process launch")):
                with self.assertRaisesRegex(BackendError, "closed"):
                    backend.complete("test", RESULT_SCHEMA, root, root)

    def test_cli_reconnection_can_recover_but_failed_turn_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "events.py"
            script.write_text('''import json, pathlib, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps({"candidates": [], "empty_reason": "Fixture"}))
print(json.dumps({"type": "error", "message": "Reconnecting"}))
if "failed" in prompt: print(json.dumps({"type": "turn.failed"}))
if "incomplete" not in prompt: print(json.dumps({"type": "turn.completed"}))
''')
            backend = CodexBackend({"command": [sys.executable, "-B", str(script)]})
            for prompt in ("recovered", "failed", "incomplete"):
                folder = root / prompt
                folder.mkdir()
                if prompt == "recovered":
                    self.assertEqual(backend.complete(prompt, RESULT_SCHEMA, folder, root)["candidates"], [])
                else:
                    with self.assertRaises(BackendError):
                        backend.complete(prompt, RESULT_SCHEMA, folder, root)


if __name__ == "__main__":
    unittest.main()
