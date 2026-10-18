"""Load built-in or user-supplied Python method files."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import re
import sys

from ..core import file_hash


def load_methods(files: list[Path] | None = None) -> dict:
    modules = [importlib.import_module(f"{__name__}.{name}") for name in
               ("local_proof", "proof_completion", "proof_repair", "reading", "reflection", "exploration")]
    for path in files or []:
        module_name = "math_generation_plugin_" + file_hash(path)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load method file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    result = {}
    for module in modules:
        name = getattr(module, "name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not callable(getattr(module, "generate", None)):
            raise ValueError("A method needs a lower-case name and generate(source, context, params)")
        if name in result or not getattr(module, "description", ""):
            raise ValueError(f"Duplicate method name or missing description: {name}")
        result[name] = module
    return result
