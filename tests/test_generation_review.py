"""Regression checks for configuration mistakes, concurrent writes, and provenance."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_generation import FakeBackend, Fixture, candidate, http_fixture
from oaimathdistill.generation.__main__ import preview_summary
from oaimathdistill.generation.backends import BackendError, ChatBackend, CodexBackend
from oaimathdistill.generation.core import RESULT_SCHEMA, text_hash, validate_result
from oaimathdistill.generation.runner import Plan, export_run, run, run_lock
from oaimathdistill.generation.sources import SourceLoader


class ReviewTests(Fixture):
    def test_configuration_mistakes_fail_before_calls_or_output(self):
        changes = [
            ('concurrency = 1', 'concurency = 4', 'Unknown run keys'),
            ('timeout = 10', 'timeot = 10', 'Unknown Codex backend keys'),
            ('params = {count = 1, form = "qa"}', 'param = {count = 1}', 'Unknown method keys'),
            ('params = {count = 1, form = "qa"}', 'params = "count = 1"', 'method.params'),
            ('name = "reading"', 'name = "reading"\nenabled = "false"', 'method.enabled'),
            ('name = "reading"', 'name = "reading"\nsources = "fragment"', 'method.sources'),
            ('source_root = "math"', 'source_root = "math"\nunknown = 1', 'Unknown configuration keys'),
        ]
        for old, new, message in changes:
            with self.subTest(message=message):
                config = self.config(['reading'], two_sources=False)
                config.write_text(config.read_text().replace(old, new))
                with self.assertRaisesRegex(ValueError, message):
                    Plan(config)
                self.assertFalse((self.root / 'runs').exists())

    def test_free_method_params_and_api_extra_remain_configurable(self):
        config = self.config(['reading'], two_sources=False)
        config.write_text(config.read_text().replace('params = {count = 1, form = "qa"}',
            'params = {count = 1, form = "qa", focus = "assumptions", difficulty = "advanced"}'))
        preview = Plan(config).preview()
        self.assertIn('"focus":"assumptions"', preview['jobs'][0]['request']['prompt'])
        backend = ChatBackend({'base_url': 'https://models.example.invalid/v1', 'model': 'fixture',
                               'extra': {'reasoning_effort': 'high', 'seed': 3}})
        self.assertEqual(backend.config['extra']['reasoning_effort'], 'high')

    def test_empty_and_ignored_source_selections_are_rejected(self):
        loader = SourceLoader(self.root, self.root / 'math')
        base = {'id': 'fragment', 'path': str(self.paper)}
        for spec in [
            {**base, 'ranges': []}, {**base, 'ranges': '1-2'},
            {**base, 'mode': 'agent', 'ranges': [[1, 1]]},
            {'id': 'literal', 'text': 'A premise.', 'ranges': [[1, 1]]},
            {**base, 'range': [[1, 1]]},
        ]:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                loader.load(spec)
        self.paper.write_text('  \n\t\n')
        with self.assertRaisesRegex(ValueError, 'non-whitespace'):
            loader.load(base)

    def test_source_digest_uses_the_same_bytes_as_the_fragment(self):
        original = self.paper.read_bytes()
        read_bytes = Path.read_bytes
        changed = False

        def changing_read(path):
            nonlocal changed
            data = read_bytes(path)
            if path.resolve() == self.paper.resolve() and not changed:
                changed = True
                path.write_bytes(b'A later, different version.\n')
            return data

        with patch.object(Path, 'read_bytes', changing_read):
            source = SourceLoader(self.root, self.root / 'math').load({'id': 'fragment', 'path': str(self.paper)})
        self.assertEqual(next(iter(source.files.values()))['sha256'], text_hash(original.decode()))
        with self.assertRaisesRegex(ValueError, 'changed'):
            source.assert_unchanged()

    def test_changed_prompt_or_config_stops_before_model_calls(self):
        config = self.config(['reading'], two_sources=False)
        prompt = self.root / 'reading.md'
        prompt.write_text('Original prompt')
        config.write_text(config.read_text() + '\n[prompts]\nreading = "reading.md"\n')
        plan = Plan(config)
        prompt.write_text('Changed prompt')
        backend = FakeBackend()
        with self.assertRaisesRegex(ValueError, 'prompt changed'):
            self.execute(plan, backend)
        self.assertEqual(backend.calls, [])
        self.assertFalse(plan.output.exists())
        plan = Plan(config)
        config.write_text(config.read_text().replace('count = 1', 'count = 2'))
        with self.assertRaisesRegex(ValueError, 'Configuration changed'):
            self.execute(plan, backend)
        self.assertEqual(backend.calls, [])

    def test_prompt_change_during_request_keeps_reply_but_rejects_checkpoint(self):
        config = self.config(['reading'], two_sources=False)
        prompt = self.root / 'reading.md'
        prompt.write_text('Original prompt')
        config.write_text(config.read_text() + '\n[prompts]\nreading = "reading.md"\n')
        plan, backend = Plan(config), FakeBackend()
        complete = backend.complete

        def change_prompt(*args):
            result = complete(*args)
            prompt.write_text('Changed while generation was active')
            return result

        backend.complete = change_prompt
        with self.assertRaisesRegex(ValueError, 'prompt changed'):
            self.execute(plan, backend)
        step = plan.output / 'jobs' / plan.jobs[0]['id'] / 'steps/generate'
        self.assertTrue((step / 'attempt-001/response.json').is_file())
        self.assertFalse((step / 'checkpoint.json').exists())
        self.assertEqual(json.loads((step / 'attempt-001/trace.json').read_text())['status'], 'failed')
        self.assertFalse((plan.output / '.run.lock').exists())

    def test_export_cannot_race_or_remove_an_active_writer_lock(self):
        plan = Plan(self.config(['reading'], two_sources=False))
        self.execute(plan, FakeBackend())
        before = (plan.output / 'train.jsonl').read_bytes()
        with run_lock(plan.output):
            with self.assertRaisesRegex(ValueError, 'locked'):
                export_run(plan.output)
            self.assertTrue((plan.output / '.run.lock').is_file())
        self.assertEqual((plan.output / 'train.jsonl').read_bytes(), before)
        self.assertFalse((plan.output / '.run.lock').exists())

    def test_unused_codex_config_does_not_require_an_installed_cli(self):
        body = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
            {'candidates': [], 'empty_reason': 'Fixture empty result.'})}}]}
        with http_fixture(body) as (url, requests):
            config = self.api_config(url)
            config.write_text(config.read_text() + '''\n[backends.unused_codex]
kind = "codex"
command = ["this-command-is-not-installed"]
''')
            plan = Plan(config)
            with patch('oaimathdistill.generation.runner.subprocess.run', side_effect=AssertionError('Unexpected CLI probe')):
                self.assertEqual(run(plan)['job_statuses'], {'empty': 1})
            self.assertEqual(len(requests), 1)

    def test_summary_preview_explains_jobs_backends_and_call_bound(self):
        plan = Plan(self.config(two_sources=False))
        preview = plan.preview()
        self.assertEqual(preview['overview']['jobs'], 6)
        self.assertEqual(preview['overview']['candidate_limit'], 6)
        self.assertEqual(preview['overview']['maximum_calls_before_retries'], 13)
        summary = preview_summary(preview)
        self.assertIn('Generation: fixture | Review: fixture', summary)
        self.assertIn('Application calls before retries: at most 13', summary)
        self.assertIn('proof_repair\tfragment\tfragment', summary)
        self.assertFalse(plan.output.exists())
        plan.output.mkdir(parents=True)
        (plan.output / 'run.json').write_text('{"fingerprint": "a-different-run"}')
        self.assertEqual(plan.preview()['overview']['output_status'], 'fingerprint_mismatch')
        self.assertIn('Choose a fresh output directory', preview_summary(plan.preview()))

    def test_version_metadata_probe_failure_does_not_abort_a_job(self):
        plan = Plan(self.config(['reading'], two_sources=False))
        backend = FakeBackend()
        with patch('oaimathdistill.generation.runner.make_backend', return_value=backend), patch(
            'oaimathdistill.generation.runner.subprocess.run', side_effect=OSError('Fixture unavailable version probe')
        ):
            self.assertEqual(run(plan)['job_statuses'], {'completed': 1})
        manifest = json.loads((plan.output / 'run.json').read_text())
        self.assertEqual(manifest['backend_versions']['fixture'], 'unavailable')

    def test_multiple_jobs_and_call_stages_have_distinct_artifacts(self):
        def reply(payload):
            reviewer = '\n\nCANDIDATE:\n' in payload['messages'][0]['content']
            result = ({'status': 'passed', 'reasons': ['Fixture only.'], 'evidence_scope': 'Transport only.'}
                      if reviewer else {'candidates': [candidate()], 'empty_reason': None})
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}

        with http_fixture(reply) as (url, requests):
            config = self.config(two_sources=False)
            config.write_text(config.read_text().replace('concurrency = 1', 'concurrency = 3').replace(
                'kind = "codex"\ncommand = ["codex"]', f'kind = "chat"\nbase_url = "{url}"\nmodel = "fixture"'))
            plan = Plan(config)
            summary = run(plan)
            self.assertEqual(summary['job_statuses'], {'completed': 6})
            self.assertEqual(summary['call_attempts'], 13)
            self.assertEqual(len(requests), 13)
            traces = [json.loads(line) for line in (plan.output / 'traces.jsonl').read_text().splitlines()]
            self.assertEqual(len({row['files']['trace.json'] for row in traces}), 13)
            self.assertTrue(all(row['status'] == 'completed' for row in traces))
            self.assertFalse(list(plan.output.rglob('*.tmp')))
            self.assertFalse((plan.output / '.run.lock').exists())

    def test_result_cannot_claim_both_candidates_and_an_empty_reason(self):
        with self.assertRaisesRegex(ValueError, 'empty_reason=null'):
            validate_result({'candidates': [candidate()], 'empty_reason': 'No usable tasks.'}, 1)


class AdapterReviewTests(unittest.TestCase):
    def test_closed_api_backend_cannot_make_another_request(self):
        backend = ChatBackend({'base_url': 'https://models.example.invalid/v1', 'model': 'fixture'})
        backend.close()
        with tempfile.TemporaryDirectory() as directory, patch(
            'urllib.request.build_opener', side_effect=AssertionError('Unexpected network after cancellation')
        ):
            with self.assertRaisesRegex(BackendError, 'closed'):
                backend.complete('test', RESULT_SCHEMA, Path(directory), Path(directory))

    def test_invalid_api_settings_fail_without_a_request(self):
        for override in [
            {'base_url': 'https://models.example.invalid/v1#fragment'},
            {'base_url': 'https://models.example.invalid:bad/v1'},
            {'token_field': 'tokens'}, {'json_mode': 'false'}, {'temperature': float('nan')},
            {'model': '  '}, {'api_key_env': ''}, {'extra': {'stream': True}},
        ]:
            with self.subTest(override=override), patch('urllib.request.build_opener', side_effect=AssertionError('Unexpected network')):
                with self.assertRaises(ValueError):
                    ChatBackend({'base_url': 'https://models.example.invalid/v1', 'model': 'fixture', **override})

    def test_cli_usage_survives_failed_or_malformed_final_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'fixture.py'
            script.write_text('''import json, pathlib, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
answer = pathlib.Path(args[args.index("--output-last-message") + 1])
answer.write_text("not JSON" if "malformed" in prompt else "{}")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 19}}))
if "failed" in prompt: print(json.dumps({"type": "turn.failed"}))
''')
            backend = CodexBackend({'command': [sys.executable, '-B', str(script)]})
            for prompt, exception in [('failed', BackendError), ('malformed', json.JSONDecodeError)]:
                folder = root / prompt
                folder.mkdir()
                with self.assertRaises(exception):
                    backend.complete(prompt, RESULT_SCHEMA, folder, root)
                self.assertEqual(json.loads((folder / 'usage.json').read_text()), [{'input_tokens': 19}])


if __name__ == '__main__':
    unittest.main()
