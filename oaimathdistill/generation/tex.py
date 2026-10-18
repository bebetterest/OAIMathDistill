"""Literal TeX heading units with static includes and original-file provenance.

This is a source reader, not a TeX interpreter. Macros and conditionals are not
executed. Ambiguous, missing, dynamic, cyclic, and escaping includes are errors.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
import re

from .core import text_hash

LEVELS = {"part": -2, "chapter": -1, "section": 0, "subsection": 1}
TOKEN = re.compile(r"\\[A-Za-z@]+|\\.|[{}]", re.S)
VERBATIM = {"verbatim", "verbatim*", "lstlisting", "minted", "comment"}


def group(text: str, position: int, opening: str = "{") -> tuple[str, int]:
    """Read a balanced literal argument, including nested and escaped braces."""
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != opening:
        raise ValueError("Expected a literal TeX argument; use explicit ranges or unit = 'file'")
    closing = "}" if opening == "{" else "]"
    start, depth, braces = position + 1, 1, 0
    position += 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if opening == "[" and char in "{}":
            braces += 1 if char == "{" else -1
        if not braces:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start:position], position + 1
        position += 1
    raise ValueError("Unbalanced TeX argument; use explicit ranges or unit = 'file'")


def masked(text: str) -> str:
    """Hide comments and verbatim material without changing character offsets."""
    result = list(text)
    position = 0
    while position < len(text):
        start = position
        if text[position] == "%":
            end = text.find("\n", position)
            position = len(text) if end < 0 else end
        elif text[position] == "\\":
            token = TOKEN.match(text, position)
            if token is None:
                position += 1
                continue
            name = token[0][1:]
            position = token.end()
            if name == "begin":
                try:
                    environment, end = group(text, position)
                except ValueError:
                    continue
                if environment not in VERBATIM:
                    continue
                ending = re.search(r"\\end\s*\{" + re.escape(environment) + r"\}", text[end:])
                if ending is None:
                    raise ValueError(f"Unclosed verbatim environment: {environment}")
                position = end + ending.end()
            elif name == "verb":
                if position < len(text) and text[position] == "*":
                    position += 1
                if position >= len(text) or text[position].isspace():
                    raise ValueError("Malformed inline TeX verbatim command")
                delimiter = text[position]
                end = text.find(delimiter, position + 1)
                if end < 0:
                    raise ValueError("Unclosed inline TeX verbatim command")
                position = end + 1
            else:
                continue
        else:
            position += 1
            continue
        result[start:position] = ["\n" if char == "\n" else " " for char in text[start:position]]
    return "".join(result)


def commands(text: str):
    """Yield literal top-level commands, ignoring commands inside macro bodies."""
    depth = 0
    for token in TOKEN.finditer(text):
        value = token[0]
        if value == "{":
            depth += 1
        elif value == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and value[1:].replace("@", "").isalpha():
            yield value[1:], token.start(), token.end()


@dataclass(frozen=True)
class Piece:
    path: Path
    start: int
    end: int


class TexDocument:
    def __init__(self, entry: Path, project: Path, allowed: set[Path] | None, initial: str):
        self.entry, self.project = entry.resolve(), project.resolve()
        self.allowed = allowed
        self.contents = {self.entry: initial}
        self.pieces = self._expand(self.entry, [])
        self.segments = []
        offset = 0
        for piece in self.pieces:
            size = piece.end - piece.start
            self.segments.append((offset, offset + size, piece))
            offset += size
        self.text = "".join(self.contents[p.path][p.start:p.end] for p in self.pieces)
        self.scan = masked(self.text)
        self.newlines = {path: [-1, *[m.start() for m in re.finditer("\n", content)]]
                         for path, content in self.contents.items()}
        self.lines = {path: content.splitlines(keepends=True) for path, content in self.contents.items()}

    def key(self, path: Path) -> str:
        return str(path.relative_to(self.project)) if path.is_relative_to(self.project) else str(path)

    def files(self) -> dict:
        return {self.key(path): {"path": self.key(path), "absolute_path": str(path),
                                "sha256": text_hash(content), "lines": len(self.lines[path])}
                for path, content in self.contents.items()}

    def _read(self, path: Path) -> str:
        if path not in self.contents:
            self.contents[path] = path.read_bytes().decode("utf-8")
        return self.contents[path]

    def _resolve(self, argument: str, parent: Path, command: str) -> Path:
        if not argument or any(char in argument for char in "\\#$%{}") or Path(argument).is_absolute():
            raise ValueError(f"Nonliteral or absolute TeX include in {self.key(parent)}: {argument}")
        candidates = set()
        bases = [self.entry.parent, parent.parent]
        for base in bases:
            candidate = (base / argument).resolve()
            if not candidate.suffix:
                candidate = candidate.with_suffix(".tex")
            permitted = (candidate in self.allowed if self.allowed is not None else
                         candidate.is_relative_to(self.entry.parent) and candidate.suffix == ".tex")
            if permitted and candidate.is_file():
                candidates.add(candidate)
        if len(candidates) != 1:
            raise ValueError(f"Unresolved, escaping, or ambiguous {command} in {self.key(parent)}: {argument}; "
                             "select a native body file, explicit ranges, or unit = 'file'")
        return candidates.pop()

    def _expand(self, path: Path, stack: list[Path]) -> list[Piece]:
        if path in stack or len(stack) >= 32:
            raise ValueError(f"Cyclic or excessively nested TeX include: {self.key(path)}")
        original = self._read(path)
        scan = masked(original)
        tokens = list(commands(scan))
        if any(name == "includeonly" for name, _, _ in tokens):
            raise ValueError("Automatic units do not evaluate includeonly; select a native body file or explicit ranges")
        start, stop = 0, len(original)
        for name, first, last in tokens:
            if name in {"begin", "end"}:
                environment, end = group(scan, last)
                if environment == "document":
                    if name == "begin":
                        start = end
                    else:
                        stop = first
                        break
        pieces, cursor = [], start
        for name, first, last in tokens:
            if first < start or first >= stop or first < cursor:
                continue
            if name == "includepdf":
                raise ValueError("A PDF assembly wrapper is not a native TeX body; select the native body file")
            if name not in {"input", "include", "subfile", "import", "subimport", "begin"}:
                continue
            if name == "begin":
                environment, end = group(scan, last)
                if environment != "thebibliography":
                    continue
                ending = re.search(r"\\end\s*\{thebibliography\}", scan[end:stop])
                if ending is None:
                    raise ValueError("Unclosed thebibliography environment")
                child, end = [], end + ending.end()
            else:
                position = last
                while position < stop and scan[position].isspace():
                    position += 1
                if position < stop and scan[position] == "{":
                    argument, end = group(scan, position)
                elif name == "input":
                    match = re.match(r"[^\s{}]+", scan[position:stop])
                    if match is None:
                        raise ValueError("Missing TeX input path")
                    argument, end = match[0], position + match.end()
                else:
                    raise ValueError("TeX include requires literal braced arguments")
                if name in {"import", "subimport"}:
                    filename, end = group(scan, end)
                    argument = str(Path(argument.strip()) / filename.strip())
                target = self._resolve(argument.strip(), path, name)
                child = self._expand(target, [*stack, path])
            pieces.append(Piece(path, cursor, first))
            pieces.extend(child)
            cursor = end
        pieces.append(Piece(path, cursor, stop))
        return [piece for piece in pieces if piece.end > piece.start]

    def units(self, requested: str) -> list[dict]:
        target = LEVELS[requested]
        headings, stack, environments = [], [], []
        for name, first, last in commands(self.scan):
            if name in {"begin", "end"}:
                environment, _ = group(self.scan, last)
                if name == "begin":
                    environments.append(environment)
                elif environment in environments:
                    del environments[len(environments) - 1 - environments[::-1].index(environment):]
                continue
            if name not in LEVELS or LEVELS[name] > target:
                continue
            position = last
            if position < len(self.scan) and self.scan[position] == "*":
                position += 1
            while position < len(self.scan) and self.scan[position].isspace():
                position += 1
            if position < len(self.scan) and self.scan[position] == "[":
                _, position = group(self.scan, position, "[")
            title, _ = group(self.scan, position)
            while stack and LEVELS[headings[stack[-1]]["kind"]] >= LEVELS[name]:
                stack.pop()
            record = {"kind": name, "title": re.sub(r"\s+", " ", title).strip(), "start": first,
                      "parents": stack.copy(), "children": [], "environments": environments.copy()}
            if stack:
                headings[stack[-1]]["children"].append(len(headings))
            headings.append(record)
            stack.append(len(headings) - 1)
        selected = [i for i, item in enumerate(headings) if not item["children"]]
        if not selected:
            return [{"start": 0, "end": len(self.text), "kind": "document", "heading_path": []}]
        units = []
        for index in selected:
            heading = headings[index]
            first = heading["start"]
            child = index
            for parent in reversed(heading["parents"]):
                if headings[parent]["children"][0] != child:
                    break
                first = headings[parent]["start"]
                child = parent
            units.append({"start": first, "kind": heading["kind"],
                          "heading_environments": heading["environments"], "heading_path": [
                {"kind": headings[i]["kind"], "title": headings[i]["title"]}
                for i in [*heading["parents"], index]]})
        units[0]["start"] = 0
        for index, unit in enumerate(units):
            unit["end"] = units[index + 1]["start"] if index + 1 < len(units) else len(self.text)
        return units

    def excerpt(self, unit: dict) -> tuple[str, list[dict]]:
        blocks = []
        for start, end, piece in self.segments:
            first, last = max(start, unit["start"]), min(end, unit["end"])
            if first >= last:
                continue
            raw_start, raw_end = piece.start + first - start, piece.start + last - start
            line_start = bisect_right(self.newlines[piece.path], raw_start - 1)
            line_end = bisect_right(self.newlines[piece.path], raw_end - 2)
            key = self.key(piece.path)
            if blocks and blocks[-1]["path"] == key and blocks[-1]["start_line"] <= line_start <= blocks[-1]["end_line"] + 1:
                blocks[-1]["end_line"] = max(blocks[-1]["end_line"], line_end)
            else:
                blocks.append({"path": key, "start_line": line_start, "end_line": line_end})
        chunks = []
        for block in blocks:
            path = self.project / block["path"]
            lines = self.lines[path]
            text = "".join(lines[block["start_line"] - 1:block["end_line"]])
            block["sha256"] = text_hash(text)
            chunks.append(f"{block['path']}:{block['start_line']}-{block['end_line']}\n{text}")
        return "\n\n".join(chunks), blocks
