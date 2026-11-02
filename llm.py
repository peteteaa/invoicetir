"""The only module that calls Fireworks (openai client, base_url and key from config).

Sends a prompt (text or page images) in JSON mode, validates the reply with Pydantic, and logs
every call to logs/calls.jsonl: doc_id, stage, model, input/output tokens, latency, cost.
Invalid JSON is returned as a failure that counts against the caller's retry budget.
"""
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel, ValidationError

import config

_client = None


def client():
    global _client
    if _client is None:
        config.require("FIREWORKS_API_KEY")
        # max_retries covers 429 rate limits, 5xx and connection errors, with exponential backoff.
        # These are API failures, not model retries, so they don't use MAX_RETRIES.
        _client = OpenAI(base_url=config.FIREWORKS_BASE_URL, api_key=config.FIREWORKS_API_KEY,
                         timeout=config.REQUEST_TIMEOUT_S, max_retries=config.API_MAX_RETRIES)
    return _client


def response_schema(schema: type[BaseModel]) -> dict:
    """The schema's JSON Schema for constrained decoding, with Decimal fields as plain numbers.

    Pydantic describes Decimal as "number or numeric string"; offering only numbers stops the
    model from emitting printed formats like "7 847,32". Pydantic still validates the reply.
    """
    def fix(node):
        if isinstance(node, dict):
            options = node.get("anyOf")
            if options:
                types = [o.get("type") for o in options]
                if "number" in types and "string" in types:
                    node.pop("anyOf")
                    node["type"] = ["number", "null"] if "null" in types else "number"
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for value in node:
                fix(value)
    s = schema.model_json_schema()
    fix(s)
    return s


@dataclass
class CallResult:
    message: object                 # the assistant message (may carry tool_calls)
    text: str                       # its content, "" if none
    parsed: Optional[BaseModel]     # set when a schema was given and the reply validated
    error: Optional[str]            # why validation failed, in words the model can act on
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: Optional[Decimal]     # None when the model's prices are not configured


def cost(model: str, input_tokens: int, output_tokens: int) -> Optional[Decimal]:
    prices = config.PRICES.get(model)
    if not prices or None in prices:
        return None
    price_in, price_out = prices
    return (price_in * input_tokens + price_out * output_tokens) / Decimal(1_000_000)


def _strip_fences(text: str) -> str:
    """Remove a ```json ... ``` wrapper if the model added one."""
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return match.group(1) if match else text


def validate(text: str, schema: type[BaseModel]):
    """Return (parsed, None) or (None, error message listing what is wrong)."""
    try:
        return schema.model_validate_json(_strip_fences(text)), None
    except ValidationError as e:
        problems = []
        for err in e.errors()[:10]:
            where = ".".join(str(p) for p in err["loc"]) or "(root)"
            problems.append(f"{where}: {err['msg']}")
        return None, "Response did not match the schema: " + "; ".join(problems)


def log(record: dict):
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.LOG_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def call(*, doc_id: str, stage: str, model: str, messages: list,
         schema: Optional[type[BaseModel]] = None, tools: Optional[list] = None,
         temperature: float = 0) -> CallResult:
    """One chat completion.

    With a schema, JSON mode is constrained to that schema, so the reply is always well-formed
    JSON of the right shape (unless cut off). Tools and JSON mode don't mix, so when tools are
    offered the reply is validated without constraint.
    """
    kwargs = {"model": model, "messages": messages, "temperature": temperature,
              "max_tokens": config.MAX_OUTPUT_TOKENS}
    if model in config.REASONING_EFFORT:
        kwargs["reasoning_effort"] = config.REASONING_EFFORT[model]
    if tools:
        kwargs["tools"] = tools
    elif schema is not None:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema.__name__, "schema": response_schema(schema)}}

    start = time.monotonic()
    error = None
    try:
        response = client().chat.completions.create(**kwargs)
    except Exception as e:  # logged, then re-raised: API failures are not retries of the model
        log({"ts": datetime.now(timezone.utc).isoformat(), "doc_id": doc_id, "stage": stage,
             "model": model, "input_tokens": 0, "output_tokens": 0,
             "latency_s": round(time.monotonic() - start, 3), "cost_usd": None,
             "ok": False, "error": f"{type(e).__name__}: {e}"})
        raise
    latency = time.monotonic() - start

    message = response.choices[0].message
    text = message.content or ""
    usage = response.usage
    input_tokens = (usage.prompt_tokens or 0) if usage else 0
    output_tokens = (usage.completion_tokens or 0) if usage else 0
    call_cost = cost(model, input_tokens, output_tokens)

    parsed = None
    if schema is not None and not message.tool_calls:
        if response.choices[0].finish_reason == "length":
            error = (f"Response was cut off at {config.MAX_OUTPUT_TOKENS} tokens. Return only the "
                     "JSON object, each field once.")
        elif text.strip():
            parsed, error = validate(text, schema)
        else:
            error = "Response was empty; return the JSON object."

    log({"ts": datetime.now(timezone.utc).isoformat(), "doc_id": doc_id, "stage": stage,
         "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
         "latency_s": round(latency, 3), "cost_usd": call_cost,
         "ok": error is None, "error": error,
         "tool_calls": len(message.tool_calls or [])})
    return CallResult(message, text, parsed, error, input_tokens, output_tokens, latency, call_cost)
