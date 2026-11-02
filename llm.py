"""The only module that calls Fireworks (openai client, base_url and key from config).

Sends a prompt (text or page images) in JSON mode, validates the reply with Pydantic, and logs
every call to logs/calls.jsonl: doc_id, stage, model, input/output tokens, latency, cost.
Invalid JSON is returned as a failure that counts against the caller's retry budget.
"""
