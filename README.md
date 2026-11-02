# fireworks-agent

A small terminal coding agent (tool-calling loop) running on Fireworks AI's serverless,
OpenAI-compatible inference API. Single file: `agent.py`. Only dependency: `openai`.

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export FIREWORKS_API_KEY=...        # or put FIREWORKS_API_KEY=... in ./.env
```

## Usage

```
.venv/bin/python agent.py --list-models          # list model IDs
.venv/bin/python agent.py --model accounts/fireworks/models/<name>
.venv/bin/python agent.py -p "add a test for foo()" --yolo
```

Flags: `--model ID`, `--list-models`, `-p/--prompt TASK`, `--yolo`, `--max-steps N` (default 30), `--cwd PATH`.

REPL commands: `/exit` (or Ctrl-D), `/reset`. Ctrl-C cancels the current turn.

## Choosing a model

Resolution order: `--model`, then `$FIREWORKS_MODEL`, then `DEFAULT_MODEL` in `agent.py`.
Run `--list-models` and pick one that supports function calling (IDs look like
`accounts/fireworks/models/<name>`).

## Tools and permissions

`read_file`, `list_dir`, `search` run automatically. `write_file`, `edit_file`, `run_bash`
ask `[y/N/a]` first (`a` = allow all this session); `--yolo` skips prompts.
Logs of every message and usage go to `logs/<timestamp>.jsonl`.

## Cost

Billed per token on Fireworks serverless; no deployment needed. A per-turn token
summary (prompt / cached / completion) is printed after each turn.
# fireworksai
