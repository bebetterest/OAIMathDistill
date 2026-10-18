"""An optional method file demonstrating a source-aware custom call."""

from oaimathdistill.generation.core import RESULT_SCHEMA
from oaimathdistill.generation.methods.common import check_result

name = "assumption_analysis"
description = "Analyze one assumption using the ordinary reflection machinery."


def generate(source, context, params):
    prompt = context.generation_prompt("reflection", source, params)
    prompt += "\nFocus on one explicitly stated assumption and its role. Return empty if none is supported."
    result = context.call("analyze", prompt, RESULT_SCHEMA,
                          validator=lambda result: check_result(source, result, params))
    # Custom methods control their student input, including any necessary context.
    for item in result["candidates"]:
        item["instruction"] = ("Source excerpt:\n\n" + source.context_text(item["evidence"]) +
                               "\n\nTask:\n" + item["instruction"])
    return result


def preview(source, context, params):
    return {"stage": "analyze", "prompt": context.generation_prompt("reflection", source, params) +
            "\nFocus on one explicitly stated assumption and its role. Return empty if none is supported.",
            "schema": RESULT_SCHEMA}
