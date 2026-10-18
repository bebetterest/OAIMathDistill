"""Inventory source coverage and preserve available native LaTeX text.

This extracts documents, not mathematical tasks. LaTeX files remain separate;
macros and input directives are preserved without attempting TeX execution.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


SOURCE_SUFFIXES = {".tex", ".bib", ".sty", ".cls"}


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True
    ).strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def catalogue_families(text: str) -> dict[str, list[str]]:
    """Read PDF links, including paths containing parentheses such as CAT(0)."""
    result: dict[str, list[str]] = {}
    family = None
    for line in text.splitlines():
        heading = re.match(r"\*\*(\d{3})\.", line)
        if heading:
            family = heading[1]
        for match in re.finditer(r"\]\((preprints/[^\n]+?\.pdf)\)", line):
            if family is None:
                raise ValueError(f"Manuscript link precedes a family: {match[1]}")
            result.setdefault(match[1], []).append(family)
    return result


def summary_families(text: str) -> dict[str, str]:
    return {
        match[2]: match[1]
        for match in re.finditer(
            r"\|\s*(\d{3})\s*\|[^\n]*?\]\((reasoning_traces/[^\n]+?\.pdf)\)",
            text,
        )
    }


def source_record(root: Path, relative: str, with_text: bool) -> dict:
    data = (root / relative).read_bytes()
    record = {"path": relative, "bytes": len(data), "sha256": sha256(data)}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        record["encoding"] = "unknown"
        if with_text:
            record["original_bytes_base64"] = base64.b64encode(data).decode("ascii")
    else:
        record["encoding"] = "utf-8"
        if with_text:
            record["text"] = text
    return record


def inventory(root: Path) -> tuple[list[dict], dict]:
    tracked = git_output(root, "ls-files", "-z").split("\0")
    tracked = [path for path in tracked if path]
    papers: dict[str, list[str]] = {}
    for path in tracked:
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "preprints":
            papers.setdefault(parts[1], []).append(path)

    families = catalogue_families((root / "CONTENTS.md").read_text())
    summaries = summary_families((root / "README.md").read_text())
    records = []
    for name, files in sorted(papers.items()):
        prefix = f"preprints/{name}/"
        readme = root / prefix / "README.md"
        notice = readme.read_text() if readme.is_file() else ""
        catalogue_pdfs = [path for path in families if path.startswith(prefix)]
        status = (
            "withdrawn"
            if re.search(r"(?mi)^#.*withdrawal notice|^\*\*Withdrawn\b", notice)
            else "current_catalogue" if catalogue_pdfs else "not_in_current_catalogue"
        )
        source_paths = sorted(path for path in files if Path(path).suffix in SOURCE_SUFFIXES)
        roots = []
        for path in source_paths:
            if Path(path).suffix == ".tex":
                text = (root / path).read_text(errors="replace")
                if re.search(r"(?m)^\s*\\documentclass\s*(?:\[[^\]]*\])?\s*\{", text):
                    roots.append(path)
        records.append({
            "id": f"manuscript:{name}",
            "kind": "manuscript",
            "name": name,
            "status": status,
            "family_ids": sorted({f for path in catalogue_pdfs for f in families[path]}),
            "catalogue_pdf_paths": catalogue_pdfs,
            "pdf_paths": sorted(path for path in files if Path(path).suffix == ".pdf"),
            "source_paths": source_paths,
            "tex_entrypoint_candidates": roots,
            "primary_format": "original_latex" if source_paths else "no_native_source",
        })
    for path in sorted(tracked):
        if not path.startswith("reasoning_traces/") or not path.endswith(".pdf"):
            continue
        tex = str(Path(path).with_suffix(".tex"))
        records.append({
            "id": f"reasoning_summary:{Path(path).stem}",
            "kind": "reasoning_summary",
            "name": Path(path).stem,
            "status": "released_summary",
            "family_ids": [summaries[path]] if path in summaries else [],
            "pdf_paths": [path],
            "source_paths": [tex] if tex in tracked else [],
            "tex_entrypoint_candidates": [tex] if tex in tracked else [],
            "primary_format": "original_latex" if tex in tracked else "no_native_source",
        })
    provenance = {
        "source_revision": git_output(root, "rev-parse", "HEAD"),
        "source_worktree_status": git_output(root, "status", "--porcelain"),
        "catalogue_pdf_links": len(families),
        "catalogue_families": len({f for values in families.values() for f in values}),
    }
    return records, provenance


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(root: Path, output: Path, extract: bool) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("The output must be outside the upstream source tree.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose an empty output directory to preserve earlier runs.")
    records, provenance = inventory(root)
    output.mkdir(parents=True, exist_ok=True)
    papers = [record for record in records if record["kind"] == "manuscript"]
    summaries = [record for record in records if record["kind"] == "reasoning_summary"]
    current = [record for record in papers if record["status"] == "current_catalogue"]
    counts = {
        "manuscript_directories": len(papers),
        "manuscript_directories_with_latex": sum(bool(r["source_paths"]) for r in papers),
        "current_catalogue_manuscripts": len(current),
        "current_catalogue_manuscripts_with_latex": sum(bool(r["source_paths"]) for r in current),
        "withdrawn_directories": sum(r["status"] == "withdrawn" for r in papers),
        "unlisted_directories": sum(r["status"] == "not_in_current_catalogue" for r in papers),
        "reasoning_summaries": len(summaries),
        "reasoning_summaries_with_latex": sum(bool(r["source_paths"]) for r in summaries),
        "source_files": sum(len(r["source_paths"]) for r in records),
        "manuscript_tex_entrypoint_counts": dict(Counter(len(r["tex_entrypoint_candidates"]) for r in papers)),
    }
    extracted = {
        "source_bundles_written": 0,
        "source_files_preserved": 0,
        "source_encoding_issues": [],
        "skipped_no_native_source": [],
    }
    with (output / "inventory.jsonl").open("w") as stream:
        for record in records:
            record["source_files"] = [source_record(root, path, False) for path in record["source_paths"]]
            if extract:
                folder = output / record["kind"] / record["name"]
                sources = [source_record(root, path, True) for path in record["source_paths"]]
                if sources:
                    save_json(folder / "latex_sources.json", {
                        **provenance,
                        "artifact_id": record["id"],
                        "status": record["status"],
                        "scope": "Original tracked TeX and bibliography files, kept separate. Macros and inputs are not expanded; non-text assets are not copied.",
                        "files": sources,
                    })
                    extracted["source_bundles_written"] += 1
                    record["extraction_status"] = "native_source_preserved"
                    record["output_directory"] = str(folder.relative_to(output))
                else:
                    record["extraction_status"] = "skipped_no_native_source"
                    extracted["skipped_no_native_source"].append(record["id"])
                extracted["source_files_preserved"] += len(sources)
                extracted["source_encoding_issues"].extend(
                    source["path"] for source in sources if source["encoding"] != "utf-8"
                )
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    report = {
        "format_version": 2,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        **provenance,
        "counts": counts,
        "text_extraction_performed": extract,
        "extraction": extracted,
        "limitations": [
            "Only native LaTeX source files are exported. Artifacts without native source are recorded and skipped.",
            "LaTeX files preserve their original boundaries and dependencies; a merged document or cleaned mathematical text is not produced.",
            "PDF paths are inventory references only; PDF files are not read, converted, or copied.",
            "Withdrawn and unlisted manuscripts retain their status; they are not approved training examples.",
        ],
    }
    save_json(output / "coverage.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("math"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--extract-text", action="store_true", help="Preserve available native source files as text bundles.")
    args = parser.parse_args()
    report = run(args.source_root, args.output_dir, args.extract_text)
    print(json.dumps({"source_revision": report["source_revision"], "counts": report["counts"], "output_dir": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
