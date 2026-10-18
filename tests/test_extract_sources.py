"""Checks for source fidelity and corpus-specific extraction edge cases."""

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oaimathdistill.extract_sources import catalogue_families, run, source_record


class SourceExtractionTests(unittest.TestCase):
    def test_catalogue_preserves_parentheses_in_pdf_paths(self):
        text = "**351. Integral fillings.**\n&emsp;[CAT(0) spaces](preprints/Sharp-fillings-in-CAT(0)-spaces/paper.pdf)\n"
        self.assertEqual(catalogue_families(text), {"preprints/Sharp-fillings-in-CAT(0)-spaces/paper.pdf": ["351"]})

    def test_sources_preserve_unicode_line_endings_and_hash(self):
        data = "% π and α\r\n\\input{sections/proof}\r\n$\\frac{x}{y}$\r\n".encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_bytes(data)
            result = source_record(root, "main.tex", True)
        self.assertEqual(result["text"].encode(), data)
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())

    def test_unknown_encoding_retains_bytes_without_silent_replacement(self):
        data = b"\\title{\xff}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_bytes(data)
            result = source_record(root, "main.tex", True)
        self.assertNotIn("text", result)
        self.assertEqual(base64.b64decode(result["original_bytes_base64"]), data)

    def test_output_cannot_modify_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "outside"):
                run(root, root / "derived", False)
            with self.assertRaisesRegex(ValueError, "outside"):
                run(root / "math", root, False)

    def test_existing_outputs_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "math"
            source.mkdir()
            output = root / "derived"
            output.mkdir()
            (output / "existing.txt").write_text("keep")
            with self.assertRaisesRegex(ValueError, "empty"):
                run(source, output, False)
            self.assertEqual((output / "existing.txt").read_text(), "keep")

    def test_native_export_skips_pdf_only_artifacts_without_reading_pdfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "math"
            source.mkdir()
            native_paper = source / "preprints/native/main.tex"
            native_paper.parent.mkdir(parents=True)
            native_paper.write_text("\\documentclass{article}\n$\\frac{x}{y}$\n")
            notice = source / "preprints/withdrawn/README.md"
            notice.parent.mkdir(parents=True)
            notice.write_text("# Withdrawal notice\n")
            native_summary = source / "reasoning_traces/native.tex"
            native_summary.parent.mkdir(parents=True)
            native_summary.write_text("\\documentclass{article}\nNative summary.\n")
            pdf_paths = [
                "preprints/native/paper.pdf",
                "preprints/withdrawn/notice.pdf",
                "reasoning_traces/native.pdf",
                "reasoning_traces/pdf-only.pdf",
            ]
            for path in pdf_paths:
                (source / path).write_bytes(b"PDF content must never be read")
            (source / "CONTENTS.md").write_text(
                "**001. Native paper.**\n[Paper](preprints/native/paper.pdf)\n"
            )
            (source / "README.md").write_text(
                "| 001 | [Summary](reasoning_traces/native.pdf) |\n"
                "| 002 | [Summary](reasoning_traces/pdf-only.pdf) |\n"
            )
            tracked = [
                str(path.relative_to(source))
                for path in source.rglob("*") if path.is_file()
            ]
            original_read_bytes = Path.read_bytes

            def read_native_only(path):
                if path.suffix == ".pdf":
                    raise AssertionError(f"Unexpected PDF read: {path}")
                return original_read_bytes(path)

            def fixture_git_output(root, *arguments):
                return {
                    ("ls-files", "-z"): "\0".join(tracked),
                    ("rev-parse", "HEAD"): "fixture-revision",
                    ("status", "--porcelain"): "",
                }[arguments]

            output = root / "derived"
            with (
                patch("oaimathdistill.extract_sources.git_output", side_effect=fixture_git_output),
                patch.object(Path, "read_bytes", read_native_only),
            ):
                report = run(source, output, True)
            records = {
                record["id"]: record
                for record in map(json.loads, (output / "inventory.jsonl").read_text().splitlines())
            }
            self.assertEqual(report["extraction"]["source_bundles_written"], 2)
            self.assertEqual(report["extraction"]["source_files_preserved"], 2)
            self.assertCountEqual(
                report["extraction"]["skipped_no_native_source"],
                ["manuscript:withdrawn", "reasoning_summary:pdf-only"],
            )
            for artifact in report["extraction"]["skipped_no_native_source"]:
                self.assertEqual(records[artifact]["primary_format"], "no_native_source")
                self.assertEqual(records[artifact]["extraction_status"], "skipped_no_native_source")
                self.assertNotIn("output_directory", records[artifact])
            self.assertEqual(records["manuscript:withdrawn"]["status"], "withdrawn")
            self.assertEqual(records["manuscript:native"]["family_ids"], ["001"])
            self.assertEqual(records["reasoning_summary:native"]["family_ids"], ["001"])
            self.assertEqual(len(list(output.glob("*/*/latex_sources.json"))), 2)
            self.assertFalse(list(output.rglob("pdf_text.*")))


if __name__ == "__main__":
    unittest.main()
