"""Read native source paths and optional fragments without executing TeX."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oaimathdistill.extract_sources import SOURCE_SUFFIXES, inventory
from .core import check_keys, require_text, text_hash, file_hash


@dataclass
class Source:
    id: str
    mode: str
    root: Path
    text: str
    blocks: list[dict]
    files: dict[str, dict]
    provenance: dict

    def snapshot(self) -> dict:
        return {"id": self.id, "mode": self.mode, "text_sha256": text_hash(self.text),
                "blocks": self.blocks, "files": self.files, "provenance": self.provenance}

    def assert_unchanged(self) -> None:
        for item in self.files.values():
            if file_hash(Path(item["absolute_path"])) != item["sha256"]:
                raise ValueError(f"Source changed during the run: {item['path']}")

    def check_evidence(self, evidence: list[dict]) -> list[dict]:
        if self.mode == "agent" and not evidence:
            raise ValueError("Agent candidates need at least one original file/line citation")
        verified = []
        for ref in evidence:
            item = self.files.get(ref["path"])
            if item is None:
                raise ValueError(f"Evidence is outside the selected native files: {ref['path']}")
            start, end = ref["start_line"], ref["end_line"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= item["lines"]:
                raise ValueError(f"Invalid source range: {ref}")
            if self.mode == "fragment" and not any(
                b["path"] == ref["path"] and b["start_line"] <= start <= end <= b["end_line"]
                for b in self.blocks
            ):
                raise ValueError("Fragment evidence extends beyond the supplied ranges")
            lines = Path(item["absolute_path"]).read_bytes().decode("utf-8").splitlines(keepends=True)
            text = "".join(lines[start - 1:end])
            verified.append({**ref, "sha256": text_hash(text), "text": text})
        return verified

    def context_text(self, evidence: list[dict] | None = None) -> str:
        if self.mode == "fragment":
            return self.text
        if evidence:
            return "\n\n".join(
                f"{ref['path']}:{ref['start_line']}-{ref['end_line']}\n{ref['text']}"
                for ref in self.check_evidence(evidence)
            )
        paths = "\n".join(f"- {p} ({f['lines']} lines)" for p, f in self.files.items())
        return (f"Read and search the selected native files under {self.root}. "
                "Choose a local passage yourself. Read only the listed files, not PDFs or other data. "
                "Return evidence paths exactly as listed and 1-based inclusive line ranges.\n" + paths)


class SourceLoader:
    def __init__(self, project: Path, source_root: Path):
        self.project = project.resolve()
        self.source_root = source_root.resolve()
        self._catalogue: tuple[list[dict], dict] | None = None

    def load(self, spec: dict[str, Any]) -> Source:
        check_keys(spec, {"id", "mode", "text", "path", "ranges"}, "source")
        require_text(spec.get("id"), "source id")
        mode = spec.get("mode", "fragment")
        if mode not in {"fragment", "agent"}:
            raise ValueError("Source mode must be fragment or agent")
        files, blocks, chunks, provenance = {}, [], [], {"attribution": "user_supplied"}
        has_text = False
        if "ranges" in spec and ("text" in spec or mode != "fragment"):
            raise ValueError("ranges requires a fragment file path; it cannot be used with text or agent mode")
        if "ranges" in spec and (not isinstance(spec["ranges"], list) or not spec["ranges"]):
            raise ValueError("ranges must be a nonempty list of line-number pairs")
        if "text" in spec:
            if mode != "fragment" or "path" in spec or not isinstance(spec["text"], str) or not spec["text"].strip():
                raise ValueError("Direct text must be a nonempty fragment without path")
            chunks.append(spec["text"])
            has_text = True
        elif "path" in spec:
            path = (self.project / require_text(spec["path"], "source path")).resolve()
            if path.is_relative_to(self.source_root):
                if self._catalogue is None:
                    self._catalogue = inventory(self.source_root)
                records, upstream = self._catalogue
                relative = path.relative_to(self.source_root)
                parts = relative.parts
                record = next((r for r in records if r["kind"] == "manuscript" and
                               len(parts) >= 2 and parts[:2] == ("preprints", r["name"])), None)
                if record is None or record["status"] != "current_catalogue":
                    raise ValueError("Only current-catalogue manuscripts are selected from the upstream tree")
                paths = [self.source_root / p for p in record["source_paths"]
                         if (self.source_root / p).resolve().is_relative_to(path) or (self.source_root / p).resolve() == path]
                provenance = {**upstream, "artifact_id": record["id"], "family_ids": record["family_ids"],
                              "status": record["status"]}
            else:
                if not path.is_file() or mode == "agent":
                    raise ValueError("External inputs must be local text fragment files")
                paths = [path]
            if not paths or (mode == "fragment" and len(paths) != 1):
                raise ValueError("Select one text file for a fragment, or native files for an agent")
            for candidate in paths:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(self.source_root) and provenance.get("artifact_id"):
                    raise ValueError("An upstream source symlink escapes the source root")
                if candidate.suffix not in SOURCE_SUFFIXES | {".txt", ".md"}:
                    raise ValueError(f"Unsupported native text format: {candidate}")
                content = candidate.read_bytes().decode("utf-8")
                lines = content.splitlines(keepends=True)
                key = str(candidate.relative_to(self.project)) if candidate.is_relative_to(self.project) else str(candidate)
                # Hash the same UTF-8 bytes used for the prompt, avoiding a second-read race.
                files[key] = {"path": key, "absolute_path": str(resolved), "sha256": text_hash(content), "lines": len(lines)}
                if mode == "fragment":
                    for pair in spec.get("ranges", [[1, len(lines)]]):
                        if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
                            raise ValueError("ranges must contain pairs of 1-based inclusive line numbers")
                        start, end = pair
                        if not 1 <= start <= end <= len(lines):
                            raise ValueError(f"Invalid fragment range: {pair}")
                        excerpt = "".join(lines[start - 1:end])
                        blocks.append({"path": key, "start_line": start, "end_line": end, "sha256": text_hash(excerpt)})
                        chunks.append(f"{key}:{start}-{end}\n{excerpt}")
                        has_text = has_text or bool(excerpt.strip())
        else:
            raise ValueError("A source needs text or path")
        if mode == "fragment" and not has_text:
            raise ValueError("Selected fragment contains no non-whitespace text")
        return Source(spec["id"], mode, self.project, "\n\n".join(chunks), blocks, files, provenance)
