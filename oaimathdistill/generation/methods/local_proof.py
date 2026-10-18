"""Construct a bounded proof task from an existing local argument."""
from .common import single, preview as _preview

name = "local_proof"
description = "Prove a source-supported local result with explicit premises."


def generate(source, context, params):
    return single(source, context, params, name)


def preview(source, context, params):
    return _preview(source, context, params, name)
