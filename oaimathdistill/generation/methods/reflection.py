"""Write grounded mathematical analysis of a local passage."""
from .common import single, preview as _preview

name = "reflection"
description = "Analyze a passage's proof strategy, assumptions, or logical boundaries."


def generate(source, context, params):
    return single(source, context, params, name)


def preview(source, context, params):
    return _preview(source, context, params, name)
