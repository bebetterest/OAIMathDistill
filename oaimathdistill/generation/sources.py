"""Read native source paths and optional fragments without executing TeX."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from oaimathdistill.extract_sources import SOURCE_SUFFIXES, inventory
from .core import check_keys, require_text, text_hash, file_hash, positive
from .tex import TexDocument


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
        self.dependencies: set[Path] = set()
        self.manifest_selections: dict[str, dict] = {}

    def load_manifest(self, spec: dict[str, Any]) -> list[Source]:
        """Select a deterministic slice of a pinned, reusable native-source manifest."""
        check_keys(spec, {"id", "mode", "manifest", "unit", "offset", "limit", "max_chars"}, "manifest source")
        require_text(spec.get("id"), "source id")
        if spec.get("mode", "fragment") != "fragment":
            raise ValueError("A manuscript manifest requires fragment mode")
        offset = spec.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("source.offset must be a nonnegative integer")
        limit = positive(spec["limit"], "source.limit", integer=True) if "limit" in spec else None
        if "max_chars" in spec:
            positive(spec["max_chars"], "source.max_chars", integer=True)
        manifest_path = (self.project / require_text(spec["manifest"], "source.manifest")).resolve()
        content = manifest_path.read_bytes().decode("utf-8")
        manifest = json.loads(content)
        check_keys(manifest, {"format_version", "source_revision", "ordering", "manuscripts", "excluded"}, "source manifest")
        if type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
            raise ValueError("Unsupported source manifest format_version")
        if manifest.get("ordering") != "manuscript_id_then_unit_index":
            raise ValueError("Source manifest ordering must be manuscript_id_then_unit_index")
        require_text(manifest.get("source_revision"), "manifest.source_revision")
        if self._catalogue is None:
            self._catalogue = inventory(self.source_root)
        if manifest["source_revision"] != self._catalogue[1]["source_revision"]:
            raise ValueError("Source manifest revision differs from the upstream checkout; update the source selection explicitly")
        entries, excluded = manifest.get("manuscripts"), manifest.get("excluded", [])
        if not isinstance(entries, list) or not entries or not isinstance(excluded, list):
            raise ValueError("Source manifest needs a nonempty manuscripts array and an excluded array")
        ids = set()
        for entry in entries:
            check_keys(entry, {"id", "path"}, "manifest manuscript")
            require_text(entry.get("id"), "manifest manuscript id")
            require_text(entry.get("path"), "manifest manuscript path")
            if entry["id"] in ids:
                raise ValueError("Source manifest manuscript ids must be unique")
            ids.add(entry["id"])
        for entry in excluded:
            check_keys(entry, {"id", "reason"}, "excluded manuscript")
            require_text(entry.get("id"), "excluded manuscript id")
            require_text(entry.get("reason"), "excluded manuscript reason")
            if entry["id"] in ids:
                raise ValueError("Source manifest exclusions must be unique and disjoint from selected manuscripts")
            ids.add(entry["id"])
        self.dependencies.add(manifest_path)
        manifest_key = str(manifest_path.relative_to(self.project)) if manifest_path.is_relative_to(self.project) else str(manifest_path)
        selected, index, manuscript_ids = [], 0, set()
        for entry in sorted(entries, key=lambda item: item["id"]):
            path = (self.source_root / entry["path"]).resolve()
            if not path.is_relative_to(self.source_root) or path.suffix != ".tex":
                raise ValueError("Source manifest paths must be native TeX inside source_root")
            child = {"id": f"{spec['id']}/{entry['id']}", "path": str(path),
                     "unit": spec.get("unit", "subsection")}
            for source in self.load_many(child):
                index += 1
                if index <= offset:
                    continue
                if "max_chars" in spec and len(source.text) > spec["max_chars"]:
                    raise ValueError(f"Corpus unit {index} exceeds max_chars; select a different window or increase the limit")
                source.provenance.setdefault("selection", {"source_id": child["id"], "requested_unit": child["unit"],
                    "unit_kind": "file", "unit_index": 1, "available_units": 1, "heading_path": [],
                    "characters": len(source.text)})
                source.provenance["selection"].update({"corpus_unit_index": index,
                    "manifest_path": manifest_key, "manifest_sha256": text_hash(content),
                    "manifest_source_revision": manifest["source_revision"]})
                selected.append(source)
                manuscript_ids.add(entry["id"])
                if limit is not None and len(selected) == limit:
                    break
            if limit is not None and len(selected) == limit:
                break
        if not selected or (limit is not None and len(selected) != limit):
            raise ValueError(f"Only {len(selected)} units available after offset {offset}; reduce the requested offset/limit")
        self.manifest_selections[spec["id"]] = {"path": manifest_key, "sha256": text_hash(content),
            "ordering": manifest["ordering"], "offset": offset, "limit": limit, "selected_units": len(selected),
            "selected_manuscripts": len(manuscript_ids), "declared_manuscripts": len(entries), "excluded": excluded}
        return selected

    def load_many(self, spec: dict[str, Any]) -> list[Source]:
        """Resolve automatic TeX units; explicit ranges/text/agent inputs stay intact."""
        if "manifest" in spec:
            return self.load_manifest(spec)
        check_keys(spec, {"id", "mode", "text", "path", "ranges", "unit", "units", "max_chars"}, "source")
        options = {key: spec[key] for key in ("unit", "units", "max_chars") if key in spec}
        automatic = spec.get("mode", "fragment") == "fragment" and "path" in spec and "ranges" not in spec
        if options and not automatic:
            raise ValueError("unit, units, and max_chars require a fragment path without ranges")
        requested = spec.get("unit", "subsection")
        if not isinstance(requested, str) or requested not in {"subsection", "section", "file"}:
            raise ValueError("source.unit must be subsection, section, or file")
        if "max_chars" in spec:
            positive(spec["max_chars"], "source.max_chars", integer=True)
        base = self.load({key: value for key, value in spec.items() if key not in options})
        path = (self.project / spec["path"]).resolve() if "path" in spec else None
        if not automatic or requested == "file" or path.suffix != ".tex":
            if "units" in spec or ("unit" in spec and requested != "file"):
                raise ValueError("Heading units require a TeX fragment path; units is unavailable with unit = 'file'")
            if "max_chars" in spec and len(base.text) > spec["max_chars"]:
                raise ValueError("Source exceeds max_chars; increase the limit or choose explicit ranges")
            return [base]
        if "units" in spec and (not isinstance(spec["units"], list) or not spec["units"] or
                                any(type(n) is not int or n < 1 for n in spec["units"]) or
                                len(set(spec["units"])) != len(spec["units"])):
            raise ValueError("source.units must be a nonempty list of distinct 1-based unit numbers")
        allowed = None
        if path.is_relative_to(self.source_root):
            records, _ = self._catalogue
            record = next(r for r in records if r["id"] == base.provenance["artifact_id"])
            allowed = {(self.source_root / p).resolve() for p in record["source_paths"]}
            # Tracked native paths may not escape the selected upstream manuscript.
            paper_root = self.source_root / "preprints" / record["name"]
            allowed = {p for p in allowed if p.is_relative_to(paper_root.resolve())}
        document = TexDocument(path, self.project, allowed, base.text.partition("\n")[2])
        units = document.units(requested)
        selected = spec.get("units", list(range(1, len(units) + 1)))
        if any(n > len(units) for n in selected):
            raise ValueError(f"source.units exceeds the {len(units)} available units in {spec['id']}")
        sources = []
        files = document.files()
        for number, unit in enumerate(units, 1):
            if number not in selected:
                continue
            text, blocks = document.excerpt(unit)
            if not text.strip() or not any(b["end_line"] >= b["start_line"] for b in blocks):
                raise ValueError("Selected TeX document body contains no usable text")
            if "max_chars" in spec and len(text) > spec["max_chars"]:
                raise ValueError(f"Unit {number} of {spec['id']} exceeds max_chars; increase the limit or select other units")
            selection = {"source_id": spec["id"], "requested_unit": requested, "unit_kind": unit["kind"],
                         "unit_index": number, "available_units": len(units), "heading_path": unit["heading_path"],
                         "heading_environments": unit.get("heading_environments", []),
                         "characters": len(text), "reader": "literal_tex_with_static_includes",
                         "boundary_scope": "complete original lines; boundary lines may contain adjacent TeX",
                         "limitations": "Macros and conditionals are not executed; cross-unit dependencies are not retrieved."}
            sources.append(Source(f"{spec['id']}::{number:04d}", "fragment", self.project, text, blocks, files,
                                  {**base.provenance, "selection": selection}))
        return sources

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
