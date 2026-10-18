"""One reading method with question-answer and explanation forms."""
from .common import single, preview as _preview

name = "reading"
description = "Understand local mathematics through QA or a connected explanation."


def validate_params(params):
    if params.get("form", "qa") not in {"qa", "explanation"}:
        raise ValueError("reading form must be qa or explanation")


def generate(source, context, params):
    validate_params(params)
    return single(source, context, params, name)


def preview(source, context, params):
    validate_params(params)
    return _preview(source, context, params, name)
