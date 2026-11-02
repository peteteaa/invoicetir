"""Repair loop around extract.py and checks.py.

On failures, re-prompt the same model with the exact failures (prompts/repair.md), up to
MAX_RETRIES; invalid JSON counts as a retry. Then escalate to ESCALATION_MODEL with the
failures. If still failing, flag for human review with the reason. Never guess.
"""
