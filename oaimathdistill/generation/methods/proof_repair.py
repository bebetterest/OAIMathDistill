"""Checkpoint corruption and repair separately; absence is a valid result."""
import json

from ..core import RESULT_SCHEMA
from .common import check_result

name = "proof_repair"
description = "Diagnose and repair a documented flaw introduced into a usable argument."


def preview(source, context, params):
    return {"stage": "draft", "prompt": context.generation_prompt("repair_draft", source, params),
            "schema": RESULT_SCHEMA, "later_stages": ["repair", "review per candidate"]}


def generate(source, context, params):
    draft = context.call("draft", context.generation_prompt("repair_draft", source, params), RESULT_SCHEMA,
                         validator=lambda value: check_result(source, value, params))
    if not draft["candidates"]:
        return draft
    prompt = context.generation_prompt(name, source, params) + "\n\nPrivate construction record:\n" + json.dumps(draft)

    def validate(value):
        check_result(source, value, params)
        originals = {item["instruction"] for item in draft["candidates"]}
        if any(item["instruction"] not in originals for item in value["candidates"]):
            raise ValueError("Repair must preserve a draft's exact student instruction")
        return value

    return context.call("repair", prompt, RESULT_SCHEMA, validator=validate)
