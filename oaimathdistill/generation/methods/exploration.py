"""Suggest explicitly uncertain, motivated mathematical directions."""
from .common import single, preview as _preview

name = "exploration"
description = "Suggest motivated connections, questions, or conjectures with uncertainty."


def generate(source, context, params):
    return single(source, context, params, name)


def preview(source, context, params):
    return _preview(source, context, params, name)
