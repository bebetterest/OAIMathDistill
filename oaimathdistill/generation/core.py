"""Small shared records, persistence helpers, and response validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def check_keys(value: Any, allowed: set[str], location: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a table/object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {location} keys: {sorted(unknown)}; allowed: {sorted(allowed)}")


def require_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a nonempty string")
    return value


def positive(value: Any, name: str, *, integer: bool = False):
    if type(value) not in ((int,) if integer else (int, float)) or not 0 < value < float("inf"):
        raise ValueError(f"{name} must be a finite positive {'integer' if integer else 'number'}")
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, value: Any) -> None:
    """Replace a complete record atomically, including after interrupted runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_jsonl(path: Path, records: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical(record) + "\n")
    temporary.replace(path)


def validate_json(value: Any, schema: dict, location: str = "$", *, depth: int = 0) -> None:
    """Validate the documented JSON Schema subset used by request contracts.

    Supported keywords: type, properties, required, additionalProperties,
    items, enum, minimum, maximum, minItems, maxItems, minLength, and metadata.
    Unknown keywords fail explicitly rather than being silently ignored.
    """
    if depth > 100:
        raise ValueError(f"{location}: JSON nesting exceeds 100 levels")
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "minimum", "maximum", "minItems", "maxItems", "minLength", "description", "title", "$schema"}
    if set(schema) - supported:
        raise ValueError(f"Unsupported schema keywords: {sorted(set(schema) - supported)}")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"null": value is None, "string": isinstance(value, str),
               "integer": type(value) is int, "number": type(value) in (int, float),
               "boolean": type(value) is bool, "array": isinstance(value, list),
               "object": isinstance(value, dict)}
    if types and not any(matches.get(t, False) for t in types):
        raise ValueError(f"{location}: expected {types}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location}: value is outside enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{location}: missing {sorted(missing)}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{location}: unexpected fields {sorted(value.keys() - properties.keys())}")
        for key in value.keys() & properties.keys():
            validate_json(value[key], properties[key], f"{location}.{key}", depth=depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValueError(f"{location}: invalid array length")
        for index, item in enumerate(value):
            validate_json(item, schema.get("items", {}), f"{location}[{index}]", depth=depth + 1)
    if isinstance(value, str) and len(value.strip()) < schema.get("minLength", 0):
        raise ValueError(f"{location}: text is empty or too short")
    if type(value) in (int, float):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise ValueError(f"{location}: number is outside bounds")


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
EVIDENCE = {"type": "array", "items": object_schema({
    "path": TEXT, "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
})}
CANDIDATE_SCHEMA = object_schema({
    "instruction": TEXT, "answer": TEXT, "evidence": EVIDENCE,
    "basis": TEXT, "uncertainty": TEXT,
})
RESULT_SCHEMA = object_schema({
    "candidates": {"type": "array", "items": CANDIDATE_SCHEMA},
    "empty_reason": {"type": ["string", "null"]},
})
REVIEW_SCHEMA = object_schema({
    "status": {"type": "string", "enum": ["passed", "rejected", "needs_review"]},
    "reasons": {"type": "array", "items": TEXT},
    "evidence_scope": TEXT,
})


def validate_result(result: dict, count: int) -> dict:
    validate_json(result, RESULT_SCHEMA)
    if len(result["candidates"]) > count:
        raise ValueError("The response exceeds the requested candidate limit")
    if not result["candidates"] and not (result["empty_reason"] or "").strip():
        raise ValueError("An empty result needs a nonempty empty_reason")
    if result["candidates"] and result["empty_reason"] is not None:
        raise ValueError("A nonempty result must use empty_reason=null")
    for item in result["candidates"]:
        if not item["instruction"].strip() or not item["answer"].strip() or not item["basis"].strip():
            raise ValueError("Each candidate needs a nonempty instruction, answer, and basis")
    return result
