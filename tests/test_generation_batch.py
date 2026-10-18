"""Offline contracts for corpus windows, per-call random seeds, and recovery."""

import json
from pathlib import Path
from unittest.mock import patch

from test_generation import Fixture, FakeBackend, candidate, http_fixture
from oaimathdistill.generation.context import Context
from oaimathdistill.generation.core import RESULT_SCHEMA, digest
from oaimathdistill.generation.runner import Plan, run
from oaimathdistill.generation.sources import SourceLoader


class SeedTests(Fixture):
    def test_seeds_match_prompt_trace_checkpoint_and_export_and_resume_without_randomness(self):
        plan = Plan(self.config(['reading', 'proof_repair'], two_sources=False))
        backend = FakeBackend()
        summary = self.execute(plan, backend)
        self.assertEqual(summary['call_attempts'], 5)
        seeds = []
        for line in (plan.output / 'traces.jsonl').read_text().splitlines():
            trace = json.loads(line)
            request = json.loads((plan.output / trace['files']['request.json']).read_text())
            checkpoint = json.loads((plan.output / trace['checkpoint']).read_text())
            seed = trace['random_seed']
            self.assertRegex(seed, r'^[0-9a-f]{32}$')
            self.assertEqual(seed, request['random_seed'])
            self.assertEqual(seed, checkpoint['random_seed'])
            self.assertTrue(request['prompt'].endswith('Random seed: ' + seed))
            self.assertEqual(checkpoint['request_signature'], digest({
                'backend': request['backend'], 'prompt': request['prompt'], 'schema': request['schema']}))
            seeds.append(seed)
        self.assertEqual(len(seeds), len(set(seeds)))
        rows = [json.loads(line) for line in (plan.output / 'train.jsonl').read_text().splitlines()]
        for row in rows:
            meta = row['meta']
            self.assertTrue(meta['generation_seeds'])
            self.assertIn(meta['review_seed'], seeds)
            self.assertTrue(set(meta['generation_seeds'].values()) <= set(seeds))
            for seed in seeds:
                self.assertNotIn(seed, json.dumps(row['messages']))
        before = (plan.output / 'traces.jsonl').read_bytes()
        with patch('oaimathdistill.generation.context.secrets.token_hex', side_effect=AssertionError('No seed on resume')):
            self.assertEqual(self.execute(plan, backend)['call_attempts'], 5)
        self.assertEqual(len(backend.calls), 5)
        self.assertEqual(before, (plan.output / 'traces.jsonl').read_bytes())

    def test_retry_uses_a_new_seed_and_reuses_the_completed_draft_seed(self):
        plan = Plan(self.config(['proof_repair'], two_sources=False))
        backend = FakeBackend(fail_stage='repair')
        self.assertEqual(self.execute(plan, backend)['job_statuses'], {'failed': 1})
        stages = plan.output / 'jobs' / plan.jobs[0]['id'] / 'steps'
        draft = json.loads((stages / 'draft/checkpoint.json').read_text())
        failed = stages / 'repair/attempt-001/request.json'
        failed_bytes = failed.read_bytes()
        failed_seed = json.loads(failed_bytes)['random_seed']
        backend.fail_stage = None
        self.assertEqual(self.execute(plan, backend, retry_failed=True)['exported'], 1)
        retried = json.loads((stages / 'repair/attempt-002/request.json').read_text())
        self.assertNotEqual(failed_seed, retried['random_seed'])
        self.assertEqual(failed_bytes, failed.read_bytes())
        self.assertEqual(draft, json.loads((stages / 'draft/checkpoint.json').read_text()))
        row = json.loads((plan.output / 'train.jsonl').read_text())
        self.assertEqual(row['meta']['generation_seeds'], {
            'draft': draft['random_seed'], 'repair': retried['random_seed']})
        self.assertEqual(len(backend.calls), 4)

    def test_seed_leaks_are_rejected_before_checkpoint_and_can_be_retried(self):
        plan = Plan(self.config(['reading'], two_sources=False))
        backend = FakeBackend()
        original_complete = backend.complete

        def leaking_complete(prompt, schema, folder, cwd):
            result = original_complete(prompt, schema, folder, cwd)
            result['candidates'][0]['answer'] += '\n' + prompt.rsplit('Random seed: ', 1)[-1]
            return result

        backend.complete = leaking_complete
        self.assertEqual(self.execute(plan, backend)['job_statuses'], {'failed': 1})
        step = plan.output / 'jobs' / plan.jobs[0]['id'] / 'steps/generate'
        self.assertFalse((step / 'checkpoint.json').exists())
        self.assertTrue((step / 'attempt-001/response.json').is_file())
        backend.complete = original_complete
        self.assertEqual(self.execute(plan, backend, retry_failed=True)['exported'], 1)
        first = json.loads((step / 'attempt-001/request.json').read_text())
        second = json.loads((step / 'attempt-002/request.json').read_text())
        self.assertNotEqual(first['random_seed'], second['random_seed'])

    def test_seed_can_be_disabled(self):
        config = self.config(['reading'], two_sources=False)
        config.write_text(config.read_text().replace('concurrency = 1', 'concurrency = 1\nprompt_seed = false'))
        plan, backend = Plan(config), FakeBackend()
        self.execute(plan, backend)
        self.assertTrue(all('Random seed:' not in prompt for prompt, _ in backend.calls))
        rows = [json.loads(line) for line in (plan.output / 'traces.jsonl').read_text().splitlines()]
        self.assertTrue(all(row['random_seed'] is None for row in rows))

    def test_preview_is_stable_and_never_draws_a_random_seed(self):
        config = self.config(['reading'], two_sources=False)
        with patch('oaimathdistill.generation.context.secrets.token_hex', side_effect=AssertionError('Preview drew randomness')):
            plan = Plan(config)
            first, second = plan.preview(), plan.preview()
            summary = plan.preview(include_prompts=False)
        self.assertEqual(first, second)
        self.assertIn('Random seed: <generated-and-recorded-for-each-new-call>', first['jobs'][0]['request']['prompt'])
        self.assertNotIn('prompt', summary['jobs'][0]['request'])
        self.assertFalse(plan.output.exists())

    def test_checkpoint_seed_tampering_is_detected_before_a_new_call(self):
        plan = Plan(self.config(['reading'], two_sources=False))
        source, backend = plan.sources['fragment'], FakeBackend()
        folder = self.root / 'calls'
        context = Context(source, backend, 'fixture', folder, {}, {})
        context.call('generate', 'A fixed request.', RESULT_SCHEMA)
        checkpoint = folder / 'steps/generate/checkpoint.json'
        stored = json.loads(checkpoint.read_text())
        stored['random_seed'] = 'f' * 32 if stored['random_seed'] != 'f' * 32 else 'e' * 32
        checkpoint.write_text(json.dumps(stored))
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            Context(source, backend, 'fixture', folder, {}, {}).call('generate', 'A fixed request.', RESULT_SCHEMA)
        self.assertEqual(len(backend.calls), 1)

    def test_fresh_runs_receive_different_seed_sets(self):
        config = self.config(['reading'], two_sources=False)
        first, second = Plan(config, Path('runs/first')), Plan(config, Path('runs/second'))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.execute(first, FakeBackend())
        self.execute(second, FakeBackend())
        def seeds(plan):
            return {json.loads(line)['random_seed'] for line in (plan.output / 'traces.jsonl').read_text().splitlines()}
        self.assertTrue(seeds(first).isdisjoint(seeds(second)))

    def test_api_payload_and_review_receive_the_recorded_seed_line(self):
        def reply(payload):
            result = ({'status': 'passed', 'reasons': ['Fixture only.'], 'evidence_scope': 'Transport only.'}
                      if payload['model'] == 'review-fixture' else {'candidates': [candidate()], 'empty_reason': None})
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        with http_fixture(reply) as (url, requests):
            plan = Plan(self.api_config(url, literal=True))
            self.assertEqual(run(plan)['exported'], 1)
            seeds = [json.loads(line)['random_seed'] for line in (plan.output / 'traces.jsonl').read_text().splitlines()]
            self.assertEqual(len(requests), 2)
            for seed, (_, _, payload) in zip(seeds, requests):
                self.assertIn('Random seed: ' + seed, payload['messages'][0]['content'])
                self.assertNotIn('seed', payload)  # Provider numeric decoding seeds are independent.


class ManifestTests(Fixture):
    def setup_manifest(self):
        self.paper.write_text('\\begin{document}\n\\section{A}\n\\subsection{First}\nA1.\n\\subsection{Second}\nA2.\n\\end{document}\n')
        other = self.root / 'math/preprints/other/main.tex'
        other.parent.mkdir(parents=True)
        other.write_text('\\begin{document}\n\\section{B}\n\\subsection{First}\nB1.\n\\subsection{Second}\nB2.\n\\end{document}\n')
        self.tracked.append('preprints/other/main.tex')
        with (self.root / 'math/CONTENTS.md').open('a') as stream:
            stream.write('**002. Other.**\n[Paper](preprints/other/paper.pdf)\n')
        self.manifest = self.root / 'manifest.json'
        data = {'format_version': 1, 'source_revision': 'fixture-revision',
                'ordering': 'manuscript_id_then_unit_index', 'manuscripts': [
                    {'id': 'b', 'path': 'preprints/other/main.tex'},
                    {'id': 'a', 'path': 'preprints/selected/main.tex'}], 'excluded': []}
        self.manifest.write_text(json.dumps(data))
        return data

    def spec(self, **options):
        return {'id': 'corpus', 'manifest': 'manifest.json', **options}

    def config_manifest(self, methods=None, offset=0, limit=3):
        self.setup_manifest()
        config = self.config(methods, two_sources=False)
        text = config.read_text().replace('concurrency = 1', 'concurrency = 1\njob_order = "source"')
        text = text.replace('id = "fragment"\nmode = "fragment"\npath = "math/preprints/selected/main.tex"\nranges = [[1, 2]]',
                            f'id = "corpus"\nmanifest = "manifest.json"\noffset = {offset}\nlimit = {limit}')
        config.write_text(text)
        return config

    def test_global_window_sorts_manuscripts_then_keeps_document_unit_order(self):
        self.setup_manifest()
        loader = SourceLoader(self.root, self.root / 'math')
        sources = loader.load_many(self.spec(offset=1, limit=2))
        self.assertEqual([s.id for s in sources], ['corpus/a::0002', 'corpus/b::0001'])
        self.assertEqual([s.provenance['selection']['corpus_unit_index'] for s in sources], [2, 3])
        self.assertIn('A2.', sources[0].text)
        self.assertIn('B1.', sources[1].text)
        self.assertIn(self.manifest.resolve(), loader.dependencies)
        selection = loader.manifest_selections['corpus']
        self.assertEqual(selection['selected_units'], 2)
        self.assertEqual(selection['selected_manuscripts'], 2)
        self.assertEqual(sources[1].provenance['family_ids'], ['002'])

    def test_manifest_rejects_invalid_revision_format_paths_and_limits(self):
        original = self.setup_manifest()
        changes = [
            {'source_revision': 'different'}, {'format_version': True}, {'format_version': 2},
            {'ordering': 'random'}, {'manuscripts': []},
            {'manuscripts': [original['manuscripts'][0]] * 2},
            {'excluded': [{'id': 'a', 'reason': 'Conflicts with selected input.'}]},
            {'manuscripts': [{'id': 'a', 'path': '../outside.tex'}]},
        ]
        for change in changes:
            self.manifest.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                SourceLoader(self.root, self.root / 'math').load_many(self.spec(limit=1))
        self.manifest.write_text(json.dumps(original))
        for options in [{'offset': True}, {'offset': -1}, {'limit': 0}, {'limit': True}, {'limit': 5},
                        {'offset': 4}, {'mode': 'agent'}, {'ranges': [[1, 2]]}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                SourceLoader(self.root, self.root / 'math').load_many(self.spec(**options))

    def test_missing_includes_fail_and_only_explicit_exclusions_are_skipped(self):
        data = self.setup_manifest()
        (self.root / 'math/preprints/other/main.tex').write_text('\\begin{document}\n\\input{missing}\n\\end{document}\n')
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            SourceLoader(self.root, self.root / 'math').load_many(self.spec())
        data['manuscripts'] = [entry for entry in data['manuscripts'] if entry['id'] == 'a']
        data['excluded'] = [{'id': 'b', 'reason': 'Unavailable native include.'}]
        self.manifest.write_text(json.dumps(data))
        loader = SourceLoader(self.root, self.root / 'math')
        self.assertEqual(len(loader.load_many(self.spec(limit=2))), 2)
        self.assertEqual(loader.manifest_selections['corpus']['excluded'], data['excluded'])

    def test_limit_and_methods_are_expanded_independently_in_source_order(self):
        methods = ['local_proof', 'proof_completion', 'proof_repair', 'reading', 'reflection', 'exploration']
        plan = Plan(self.config_manifest(methods, offset=1, limit=2))
        self.assertEqual(len(plan.sources), 2)
        self.assertEqual(len(plan.jobs), 12)
        self.assertEqual([j['method'] for j in plan.jobs[:6]], methods)
        self.assertEqual(len({j['source_id'] for j in plan.jobs[:6]}), 1)
        self.assertTrue(all(j['params']['count'] == 1 for j in plan.jobs))
        preview = plan.preview(include_prompts=False)
        self.assertEqual(preview['overview']['candidate_limit'], 12)
        self.assertEqual(preview['overview']['maximum_calls_before_retries'], 26)
        self.assertFalse(plan.output.exists())

    def test_manifest_is_fingerprinted_and_rechecked_before_calls(self):
        config = self.config_manifest(['reading'])
        plan = Plan(config)
        original = plan.fingerprint
        data = json.loads(self.manifest.read_text())
        data['excluded'] = [{'id': 'c', 'reason': 'Explicit selection metadata.'}]
        self.manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'changed'):
            plan.assert_implementation_unchanged()
        self.assertNotEqual(original, Plan(config).fingerprint)

    def test_character_guard_applies_only_to_selected_units(self):
        self.setup_manifest()
        self.paper.write_text(self.paper.read_text().replace('A1.', 'x' * 2000))
        loader = SourceLoader(self.root, self.root / 'math')
        selected = loader.load_many(self.spec(offset=1, limit=1, max_chars=200))
        self.assertEqual(selected[0].provenance['selection']['corpus_unit_index'], 2)
        with self.assertRaisesRegex(ValueError, 'max_chars'):
            loader.load_many(self.spec(offset=0, limit=1, max_chars=200))

    def test_manifest_allows_an_explicit_whole_file_override(self):
        self.setup_manifest()
        sources = SourceLoader(self.root, self.root / 'math').load_many(self.spec(unit='file', limit=2))
        self.assertEqual(len(sources), 2)
        self.assertEqual([s.provenance['selection']['unit_kind'] for s in sources], ['file', 'file'])
