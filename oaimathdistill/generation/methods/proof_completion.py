"""Construct a meaningful missing-step task."""
from .common import single, preview as _preview

name = "proof_completion"
description = "Complete one meaningful consecutive gap in a local derivation."


def generate(source, context, params):
    return single(source, context, params, name)


def preview(source, context, params):
    return _preview(source, context, params, name)
