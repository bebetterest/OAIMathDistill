"""Offline checks for semantic TeX units, DeepSeek payloads, and local credentials."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_generation import Fixture, candidate, http_fixture
from oaimathdistill.generation.backends import BackendError, ChatBackend
from oaimathdistill.generation.core import file_hash, text_hash
from oaimathdistill.generation.runner import Plan, run
from oaimathdistill.generation.sources import SourceLoader


BODY = r'''\documentclass{article}
\newcommand{\fake}{\subsection{Preamble heading}}
\begin{document}
Introductory context.
\section{Setup}
Parent context.
\subsection[Short {title}]{First {nested} title}
Assume $x>0$.
\begin{proof}
Then $x/x=1$.
\end{proof}
\subsection*{Second}
Another complete argument.
\subsubsection{An internal detail}
This remains in the second subsection.
\section{Standalone}
An unsplit section without subsections.
\end{document}
'''


class UnitTests(Fixture):
    def loader(self, text=BODY):
        self.paper.write_text(text)
        return SourceLoader(self.root, self.root / 'math')

    def test_subsections_and_section_fallback_preserve_complete_proofs(self):
        loader = self.loader()
        original = self.paper.read_bytes()
        sources = loader.load_many({'id': 'paper', 'path': str(self.paper)})
        self.assertEqual([s.id for s in sources], ['paper::0001', 'paper::0002', 'paper::0003'])
        self.assertEqual([s.provenance['selection']['unit_kind'] for s in sources],
                         ['subsection', 'subsection', 'section'])
        first, second, third = sources
        self.assertIn('Parent context.', first.text)
        self.assertIn('Introductory context.', first.text)
        self.assertIn(r'\begin{proof}', first.text)
        self.assertIn(r'\end{proof}', first.text)
        self.assertNotIn('Second', first.text)
        self.assertIn('An internal detail', second.text)
        self.assertNotIn('Parent context.', second.text)
        self.assertNotIn('Standalone', second.text)
        self.assertIn('unsplit section', third.text)
        self.assertNotIn('documentclass', first.text)
        self.assertNotIn(r'\end{document}', third.text)
        self.assertEqual(first.provenance['selection']['heading_path'][-1]['title'], 'First {nested} title')
        for source in sources:
            for block in source.blocks:
                ref = {key: block[key] for key in ('path', 'start_line', 'end_line')}
                self.assertEqual(source.check_evidence([ref])[0]['sha256'], block['sha256'])
            source.assert_unchanged()
        self.assertEqual(original, self.paper.read_bytes())

    def test_comments_verbatim_macros_and_escaped_commands_are_not_headings(self):
        text = r'''\begin{document}
% \subsection{Comment}
\section{Actual}
\newcommand{\fake}{\subsection{Macro body}}
\verb|\subsection{Inline verbatim}|
\begin{verbatim}
\subsection{Block verbatim} % a percent does not hide the closing environment
\end{verbatim}
\\subsection{A line break followed by text}
A 10\% value. % \subsection{Another comment}
\subsection{Real}
Real content.
\end{document}
'''
        sources = self.loader(text).load_many({'id': 'x', 'path': str(self.paper)})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].provenance['selection']['heading_path'][-1]['title'], 'Real')

    def test_unit_selection_section_override_and_file_escape_hatch(self):
        loader = self.loader()
        spec = {'id': 'x', 'path': str(self.paper)}
        selected = loader.load_many({**spec, 'units': [3, 1]})
        self.assertEqual([s.id for s in selected], ['x::0001', 'x::0003'])
        self.assertEqual(selected[1].provenance['selection']['available_units'], 3)
        sections = loader.load_many({**spec, 'unit': 'section'})
        self.assertEqual(len(sections), 2)
        self.assertIn('Second', sections[0].text)
        full = loader.load_many({**spec, 'unit': 'file'})
        self.assertEqual([s.id for s in full], ['x'])
        self.assertIn('documentclass', full[0].text)

    def test_explicit_ranges_and_direct_text_are_not_automatically_split(self):
        loader = self.loader()
        ranges = loader.load_many({'id': 'x', 'path': str(self.paper), 'ranges': [[7, 11], [12, 15]]})
        self.assertEqual([s.id for s in ranges], ['x'])
        self.assertEqual(len(ranges[0].blocks), 2)
        literal = loader.load_many({'id': 'literal', 'text': BODY})
        self.assertEqual(len(literal), 1)
        self.assertEqual(literal[0].text, BODY)
        txt = self.root / 'excerpt.txt'
        txt.write_text(BODY)
        self.assertEqual(len(loader.load_many({'id': 'txt', 'path': str(txt)})), 1)

    def test_settings_reject_silent_ignoring_and_invalid_indices(self):
        loader = self.loader()
        base = {'id': 'x', 'path': str(self.paper)}
        invalid = [
            {'id': 'literal', 'text': BODY, 'unit': 'subsection'},
            {**base, 'mode': 'agent', 'unit': 'subsection'},
            {**base, 'ranges': [[1, 2]], 'unit': 'subsection'},
            {**base, 'unit': 'file', 'units': [1]},
            {**base, 'units': []}, {**base, 'units': [True]},
            {**base, 'units': [1, 1]}, {**base, 'units': [0]},
            {**base, 'units': '1'}, {**base, 'units': [4]},
            {**base, 'unit': 'paragraph'}, {**base, 'unit': []},
            {**base, 'max_chars': -1},
        ]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                loader.load_many(spec)
        txt = self.root / 'excerpt.txt'
        txt.write_text('A text file.')
        with self.assertRaises(ValueError):
            loader.load_many({'id': 'txt', 'path': str(txt), 'unit': 'subsection'})

    def test_character_guard_rejects_instead_of_truncating(self):
        loader = self.loader()
        before = self.paper.read_bytes()
        with self.assertRaisesRegex(ValueError, 'exceeds max_chars'):
            loader.load_many({'id': 'x', 'path': str(self.paper), 'units': [2], 'max_chars': 20})
        self.assertEqual(before, self.paper.read_bytes())

    def test_static_includes_keep_original_lines_and_all_consulted_hashes(self):
        loader = self.loader(r'''\begin{document}
\section{Strategies}
Shared context.
\input{first}
\input{second}
\end{document}
''')
        first, second = self.paper.parent / 'first.tex', self.paper.parent / 'second.tex'
        first.write_bytes(b'\\subsection{First}\r\n\\begin{proof}\r\nA full proof.\r\n\\end{proof}\r\n')
        second.write_text('\\subsection{Second}\nAnother proof.\n')
        self.tracked.extend([str(p.relative_to(self.root / 'math')) for p in (first, second)])
        sources = loader.load_many({'id': 'x', 'path': str(self.paper)})
        self.assertEqual(len(sources), 2)
        self.assertIn('A full proof.\r\n', sources[0].text)
        block = next(b for b in sources[0].blocks if b['path'].endswith('first.tex'))
        self.assertEqual((block['start_line'], block['end_line']), (1, 4))
        self.assertEqual(block['sha256'], text_hash(first.read_bytes().decode()))
        self.assertNotIn('A full proof.', sources[1].text)
        with self.assertRaisesRegex(ValueError, 'beyond'):
            sources[1].check_evidence([{'path': block['path'], 'start_line': 1, 'end_line': 4}])
        second.write_text('A changed included source.\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            sources[0].assert_unchanged()

    def test_import_unbraced_input_and_external_local_includes(self):
        self.loader()
        entry = self.root / 'external' / 'main.tex'
        entry.parent.mkdir()
        (entry.parent / 'parts').mkdir()
        entry.write_text('\\begin{document}\n\\import{parts/}{one}\n\\input two\n\\end{document}\n')
        (entry.parent / 'parts' / 'one.tex').write_text('\\subsection{Imported}\nFirst.\n')
        (entry.parent / 'two.tex').write_text('\\subsection{Plain}\nSecond.\n')
        sources = SourceLoader(self.root, self.root / 'math').load_many({'id': 'x', 'path': str(entry)})
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].provenance['attribution'], 'user_supplied')
        self.assertIn('First.', sources[0].text)
        self.assertIn('Second.', sources[1].text)

    def test_missing_dynamic_cyclic_and_escaping_includes_are_errors(self):
        for command in [r'\input{missing}', r'\input{\generated}', r'\input{main}', r'\input{../../../outside}']:
            with self.subTest(command=command):
                loader = self.loader('\\begin{document}\n' + command + '\n\\end{document}\n')
                with self.assertRaises(ValueError):
                    loader.load_many({'id': 'x', 'path': str(self.paper)})

    def test_ambiguous_relative_include_is_rejected(self):
        self.loader('\\begin{document}\n\\input{parts/child}\n\\end{document}\n')
        folder = self.paper.parent / 'parts'
        folder.mkdir()
        (folder / 'child.tex').write_text('\\input{shared}\n')
        for p in (self.paper.parent / 'shared.tex', folder / 'shared.tex'):
            p.write_text('Some native content.\n')
        self.tracked.extend(str(p.relative_to(self.root / 'math')) for p in folder.glob('*.tex'))
        self.tracked.append(str((self.paper.parent / 'shared.tex').relative_to(self.root / 'math')))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            SourceLoader(self.root, self.root / 'math').load_many({'id': 'x', 'path': str(self.paper)})

    def test_bibliography_is_excluded_and_appendix_is_preserved(self):
        text = r'''\begin{document}
\section{Result}
A result.
\begin{thebibliography}{9}
\bibitem{a} A bibliography entry.
\end{thebibliography}
\appendix
\section{Appendix}
An appendix proof.
\end{document}
'''
        sources = self.loader(text).load_many({'id': 'x', 'path': str(self.paper)})
        self.assertEqual(len(sources), 2)
        self.assertNotIn('bibliography entry', '\n'.join(s.text for s in sources))
        self.assertIn('appendix proof', sources[1].text)

    def test_no_heading_falls_back_to_document_body_and_empty_body_is_rejected(self):
        loader = self.loader('\\begin{document}\nA body without headings.\n\\end{document}\n')
        sources = loader.load_many({'id': 'x', 'path': str(self.paper)})
        self.assertEqual(sources[0].provenance['selection']['unit_kind'], 'document')
        loader = self.loader('\\begin{document}\\end{document}')
        with self.assertRaisesRegex(ValueError, 'no usable text'):
            loader.load_many({'id': 'x', 'path': str(self.paper)})

    def test_plan_expands_groups_and_can_select_an_individual_unit(self):
        self.loader()
        config = self.config(['reading'], two_sources=False)
        config.write_text(config.read_text().replace('ranges = [[1, 2]]\n', ''))
        plan = Plan(config)
        self.assertEqual(len(plan.jobs), 3)
        self.assertEqual(plan.source_groups, {'fragment': ['fragment::0001', 'fragment::0002', 'fragment::0003']})
        self.assertEqual(plan.preview()['overview']['maximum_calls_before_retries'], 6)
        self.assertFalse(plan.output.exists())
        config.write_text(config.read_text().replace('name = "reading"', 'name = "reading"\nsources = ["fragment::0002"]'))
        self.assertEqual([j['source_id'] for j in Plan(config).jobs], ['fragment::0002'])

    def test_author_subsections_inside_a_long_proof_are_preserved_and_labelled(self):
        text = "\\begin{document}\n\\section{An argument}\n\\begin{proof}\n\\subsection{First step}\nOne step.\n\\subsection{Second step}\nAnother step.\n\\end{proof}\n\\end{document}\n"
        sources = self.loader(text).load_many({'id': 'x', 'path': str(self.paper)})
        self.assertEqual(len(sources), 2)
        self.assertEqual([s.provenance['selection']['heading_environments'] for s in sources], [['proof'], ['proof']])
        self.assertNotIn('Another step.', sources[0].text)
        self.assertIn('Another step.', sources[1].text)

    def test_automatic_units_reject_includeonly_and_pdf_wrappers(self):
        for text in [
            "\\includeonly{one}\n\\begin{document}\n\\section{Content}\nBody.\n\\end{document}\n",
            "\\begin{document}\n\\includepdf{paper.pdf}\n\\end{document}\n",
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.loader(text).load_many({'id': 'x', 'path': str(self.paper)})

    def test_changed_source_or_selection_changes_fingerprint(self):
        self.loader()
        config = self.config(['reading'], two_sources=False)
        config.write_text(config.read_text().replace('ranges = [[1, 2]]', 'units = [1]'))
        first = Plan(config).fingerprint
        self.paper.write_text(BODY.replace('Another complete argument.', 'A changed other subsection.'))
        self.assertNotEqual(first, Plan(config).fingerprint)
        config.write_text(config.read_text().replace('units = [1]', 'units = [2]'))
        self.assertNotEqual(first, Plan(config).fingerprint)


class CredentialTests(unittest.TestCase):
    def backend(self, **options):
        return ChatBackend({'kind': 'chat', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
                            'api_key_env': 'TEST_OAIMATH_KEY', 'env_file': '.env', **options})

    def test_dotenv_quotes_export_comments_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            env = root / '.env'
            for assignment in ['TEST_OAIMATH_KEY=fixture-value', 'export TEST_OAIMATH_KEY = "fixture-value" # comment',
                               "TEST_OAIMATH_KEY='fixture-value'", 'TEST_OAIMATH_KEY=fixture-value # comment']:
                env.write_text('UNRELATED=ignored\n' + assignment + '\n')
                self.assertEqual(self.backend().credential(root), 'fixture-value')
                self.assertNotIn('TEST_OAIMATH_KEY', os.environ)
            with patch.dict(os.environ, {'TEST_OAIMATH_KEY': 'environment-value'}):
                self.assertEqual(self.backend().credential(root), 'environment-value')

    def test_invalid_header_credentials_fail_without_echoing_the_value(self):
        for secret in ['fixture-secret\r\ninjected: value', 'fixture secret', 'non-ascii-秘密']:
            with patch.dict(os.environ, {'TEST_OAIMATH_KEY': secret}), self.assertRaises(BackendError) as error:
                self.backend().credential(Path('.'))
            self.assertNotIn(secret, str(error.exception))

    def test_invalid_or_missing_credentials_fail_without_value_leaks(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            with self.assertRaisesRegex(BackendError, 'Missing credential file'):
                self.backend().credential(root)
            for value in ['', 'TEST_OAIMATH_KEY=', 'TEST_OAIMATH_KEY="secret-value',
                          'TEST_OAIMATH_KEY=secret-value other', 'TEST_OAIMATH_KEY=a\nTEST_OAIMATH_KEY=b']:
                (root / '.env').write_text(value)
                with self.assertRaises(BackendError) as error:
                    self.backend().credential(root)
                self.assertNotIn('secret-value', str(error.exception))
            with self.assertRaisesRegex(ValueError, 'requires api_key_env'):
                ChatBackend({'base_url': 'http://localhost:8000', 'model': 'fixture', 'env_file': '.env'})


class ThinkingIntegrationTests(Fixture):
    def test_subsection_api_generation_review_thinking_and_dotenv_trajectories(self):
        self.paper.write_text(BODY)
        secret = 'fixture-secret-for-redaction'
        (self.root / '.env').write_text('DEEPSEEK_API_KEY="' + secret + '"\n')
        responses = [
            {'candidates': [candidate([{'path': 'math/preprints/selected/main.tex', 'start_line': 8, 'end_line': 10}])], 'empty_reason': None},
            {'status': 'passed', 'reasons': ['Fixture review.'], 'evidence_scope': 'Transport only.'},
        ]
        calls = []

        def reply(payload):
            result = responses[len(calls)]
            calls.append(payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result),
                    'reasoning_content': 'Provider thinking for ' + secret}}],
                    'usage': {'total_tokens': 42, 'completion_tokens_details': {'reasoning_tokens': 10}}}

        with http_fixture(reply) as (url, requests), patch.dict(os.environ, {}, clear=True):
            config = self.config(['reading'], two_sources=False)
            text = config.read_text().replace('kind = "codex"\ncommand = ["codex"]',
                f'kind = "chat"\nbase_url = "{url}"\nmodel = "deepseek-flash"\napi_key_env = "DEEPSEEK_API_KEY"\nenv_file = ".env"')
            text = text.replace('ranges = [[1, 2]]', 'units = [1]')
            text += '\n[backends.fixture.extra]\nthinking = {type = "enabled"}\nreasoning_effort = "high"\n'
            config.write_text(text)
            plan = Plan(config)
            before = file_hash(self.paper)
            self.assertEqual(run(plan)['exported'], 1)
            self.assertEqual(len(requests), 2)
            for _, headers, payload in requests:
                self.assertEqual(headers['Authorization'], 'Bearer ' + secret)
                self.assertEqual(payload['model'], 'deepseek-flash')
                self.assertEqual(payload['thinking'], {'type': 'enabled'})
                self.assertEqual(payload['reasoning_effort'], 'high')
            traces = [json.loads(line) for line in (plan.output / 'traces.jsonl').read_text().splitlines()]
            self.assertEqual({t['purpose'] for t in traces}, {'generation', 'review'})
            for trace in traces:
                raw = json.loads((plan.output / trace['files']['http_response.json']).read_text())
                self.assertEqual(raw['choices'][0]['message']['reasoning_content'], 'Provider thinking for [REDACTED]')
                self.assertEqual(raw['usage']['completion_tokens_details']['reasoning_tokens'], 10)
            for path in plan.output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret, path.read_text())
            self.assertNotIn('Provider thinking', (plan.output / 'train.jsonl').read_text())
            self.assertEqual(before, file_hash(self.paper))
            self.assertEqual(run(plan)['exported'], 1)
            self.assertEqual(len(requests), 2)
