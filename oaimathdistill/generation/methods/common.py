"""One-call methods and the default source-aware preview."""

from ..core import RESULT_SCHEMA, validate_result


def check_result(source, result, params):
    validate_result(result, params["count"])
    for item in result["candidates"]:
        source.check_evidence(item["evidence"])
    source.assert_unchanged()
    return result


def single(source, context, params, method):
    return context.call("generate", context.generation_prompt(method, source, params), RESULT_SCHEMA,
                        validator=lambda value: check_result(source, value, params))


def preview(source, context, params, method):
    return {"stage": "generate", "prompt": context.generation_prompt(method, source, params), "schema": RESULT_SCHEMA}
