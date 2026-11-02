#!/usr/bin/env python3
"""Minimal terminal coding agent (tool-calling loop) on Fireworks AI's OpenAI-compatible API."""
import argparse, datetime, fnmatch, json, os, platform, re, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = None  # e.g. "accounts/fireworks/models/<name>"; or use --model / FIREWORKS_MODEL
MAX_RESULT = 20_000
MAX_CONTEXT_CHARS = 400_000
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
DIM, RESET = "\033[2m", "\033[0m"

CWD = os.getcwd()
YOLO = False
ALLOW_ALL = False


def load_dotenv():
    path = os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


# ---------------------------------------------------------------- tools
def _p(path):
    return os.path.join(CWD, os.path.expanduser(path or "."))  # absolute path overrides CWD


def _trunc(s):
    s = str(s)
    if len(s) > MAX_RESULT:
        return s[:MAX_RESULT] + f"\n[truncated {len(s) - MAX_RESULT} chars]"
    return s


def _safe(fn):
    def wrapper(**kw):
        try:
            return _trunc(fn(**kw))
        except Exception as e:  # tools never raise; the model gets the error
            return f"error: {type(e).__name__}: {e}"
    wrapper.__name__ = fn.__name__
    return wrapper


@_safe
def read_file(path, offset=1, limit=2000):
    offset = max(1, int(offset or 1))
    limit = int(limit or 2000)
    with open(_p(path), encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return f"(no lines; file has {len(lines)} lines)"
    out = "".join(f"{i}\t{l.rstrip(chr(10))}\n" for i, l in enumerate(chunk, offset))
    end = offset - 1 + len(chunk)
    if end < len(lines):
        out += f"[{len(lines) - end} more lines; use offset={end + 1}]\n"
    return out


@_safe
def list_dir(path="."):
    names = sorted(os.listdir(_p(path)))
    return "\n".join(n + "/" if os.path.isdir(os.path.join(_p(path), n)) else n
                     for n in names if n not in SKIP_DIRS) or "(empty)"


@_safe
def search(pattern, path=".", glob=None):
    rx = re.compile(pattern)
    root = _p(path)
    files = []
    if os.path.isfile(root):
        files = [root]
    else:
        for d, dirs, fs in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            files += [os.path.join(d, f) for f in sorted(fs) if not glob or fnmatch.fnmatch(f, glob)]
    out = []
    for fp in files:
        try:
            with open(fp, "rb") as fb:
                if b"\0" in fb.read(4096):
                    continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line):
                        out.append(f"{os.path.relpath(fp, CWD)}:{n}: {line.rstrip()[:300]}")
                        if len(out) >= 200:
                            return "\n".join(out) + "\n[capped at 200 matches]"
        except OSError:
            continue
    return "\n".join(out) or "no matches"


@_safe
def write_file(path, content):
    fp = _p(path)
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content.encode())} bytes to {path}"


@_safe
def edit_file(path, old_string, new_string):
    fp = _p(path)
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    n = text.count(old_string) if old_string else 0
    if n == 0:
        return "error: old_string not found"
    if n > 1:
        return f"error: old_string not unique ({n} matches); add more context"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text.replace(old_string, new_string, 1))
    return f"edited {path}"


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)  # whole group, so children of the shell die too
    except (ProcessLookupError, PermissionError):
        pass


@_safe
def run_bash(command, timeout=120):
    timeout = float(timeout or 120)
    # Own process group: a timeout or Ctrl-C kills everything the command started.
    p = subprocess.Popen(command, shell=True, cwd=CWD, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
        return f"{out}\nerror: timed out after {timeout:g}s (process killed)"
    except KeyboardInterrupt:
        _kill_group(p)
        raise
    return f"{out}\n[exit code {p.returncode}]"


def _schema(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}

_S, _I = {"type": "string"}, {"type": "integer"}
TOOLS = [
    _schema("read_file", "Read a text file with 1-based line numbers (default up to 2000 lines).",
            {"path": _S, "offset": {**_I, "description": "first line, 1-based"}, "limit": {**_I, "description": "max lines"}}, ["path"]),
    _schema("list_dir", "List directory entries; directories end with '/'.", {"path": _S}, []),
    _schema("search", "Regex search over text files. Returns file:line: text (max 200).",
            {"pattern": _S, "path": _S, "glob": {**_S, "description": "filename glob, e.g. *.py"}}, ["pattern"]),
    _schema("write_file", "Create or overwrite a file (creates parent dirs).", {"path": _S, "content": _S}, ["path", "content"]),
    _schema("edit_file", "Replace one exact, unique occurrence of old_string with new_string.",
            {"path": _S, "old_string": _S, "new_string": _S}, ["path", "old_string", "new_string"]),
    _schema("run_bash", "Run a shell command in the working directory; returns output and exit code.",
            {"command": _S, "timeout": {**_I, "description": "seconds, default 120"}}, ["command"]),
]
IMPL = {f.__name__: f for f in (read_file, list_dir, search, write_file, edit_file, run_bash)}
NEEDS_PERMISSION = {"write_file", "edit_file", "run_bash"}


def summarize(name, a):
    if name == "write_file":
        return f"{a.get('path')} ({len(str(a.get('content', '')).encode())} bytes)"
    if name == "edit_file":
        return f"{a.get('path')}\n  - {str(a.get('old_string'))[:300]!r}\n  + {str(a.get('new_string'))[:300]!r}\n"
    if name == "run_bash":
        return str(a.get("command"))
    return ", ".join(str(v) for v in a.values())


def approved(name, args):
    global ALLOW_ALL
    if YOLO or ALLOW_ALL or name not in NEEDS_PERMISSION:
        return True
    ans = input(f"Allow {name} {summarize(name, args)}? [y/N/a] ").strip().lower()
    if ans == "a":
        ALLOW_ALL = True
    return ans in ("y", "a")


def run_tool(name, raw_args):
    fn = IMPL.get(name)
    if not fn:
        return f"error: unknown tool {name}"
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        return f"error: malformed tool arguments ({e})"
    label = (summarize(name, args).splitlines() or [""])[0] if args else ""
    print(f"{DIM}⏺ {name}({label[:100]}){RESET}")
    if not approved(name, args):
        return "user declined"
    try:
        return fn(**args)
    except TypeError as e:  # wrong/missing argument names
        return f"error: bad arguments: {e}"


# ---------------------------------------------------------------- loop
def system_prompt():
    return (f"You are a concise coding agent running in a terminal. Working directory: {CWD}. "
            f"OS: {platform.platform()}. Date: {datetime.date.today()}.\n"
            "Use the tools to inspect the project before editing. Prefer edit_file over write_file for "
            "existing files. Verify changes when practical (e.g. run tests). Keep answers short.")


def trim_context(messages):
    """Blank the oldest tool outputs until the serialized history fits the budget."""
    size = len(json.dumps(messages, default=str))
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=0)
    for i, m in enumerate(messages[:last_user]):
        if size <= MAX_CONTEXT_CHARS:
            break
        if m["role"] == "tool" and m["content"] != "[old tool output removed]":
            size -= len(m["content"]) - len("[old tool output removed]")
            m["content"] = "[old tool output removed]"


def call_model(client, model, messages):
    import openai
    for attempt in range(4):
        try:
            return client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as e:
            if attempt == 3:
                raise
            wait = 2 ** (attempt + 1)
            print(f"{DIM}transient error ({type(e).__name__}); retrying in {wait}s{RESET}")
            time.sleep(wait)


def assistant_dict(msg):
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                           for tc in msg.tool_calls]
    return d


def close_dangling(messages, log, reason):
    """Give every unanswered tool call a result, so the history stays valid for the next API call."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "assistant":
            answered = {x["tool_call_id"] for x in messages[i + 1:] if x["role"] == "tool"}
            for tc in m.get("tool_calls", []):
                if tc["id"] not in answered:
                    t = {"role": "tool", "tool_call_id": tc["id"], "content": f"not run: {reason}"}
                    messages.append(t); log(t)
            return


def run_turn(client, model, messages, log, max_steps):
    totals = [0, 0, 0]
    final = ""
    try:
        for _ in range(max_steps):
            trim_context(messages)
            resp = call_model(client, model, messages)
            u = resp.usage
            if u:
                cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                totals[0] += u.prompt_tokens or 0
                totals[1] += cached
                totals[2] += u.completion_tokens or 0
                log({"usage": {"prompt": u.prompt_tokens, "cached": cached, "completion": u.completion_tokens}})
            msg = resp.choices[0].message
            m = assistant_dict(msg)
            messages.append(m); log(m)
            if msg.content:
                print(msg.content)
            if not msg.tool_calls:
                final = msg.content or ""
                break
            for tc in msg.tool_calls:
                t = {"role": "tool", "tool_call_id": tc.id, "content": run_tool(tc.function.name, tc.function.arguments)}
                messages.append(t); log(t)
        else:
            print(f"[stopped after {max_steps} model calls; use --max-steps to raise the cap]")
    except KeyboardInterrupt:
        close_dangling(messages, log, "interrupted by the user")
        print("\n[interrupted]")
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}")
        close_dangling(messages, log, f"error: {type(e).__name__}")
    print(f"{DIM}tokens: prompt {totals[0]} / cached {totals[1]} / completion {totals[2]}{RESET}")
    return final


def list_models(client):
    rows = []
    for m in client.models.list():
        extra = ""
        for k in ("supports_tools", "supports_function_calling", "tools"):
            v = getattr(m, k, None) or (getattr(m, "model_extra", None) or {}).get(k)
            if v is not None:
                extra = f"  {k}={v}"
                break
        rows.append(f"{m.id}{extra}")
    print("\n".join(sorted(rows)))


def main():
    global CWD, YOLO
    load_dotenv()
    ap = argparse.ArgumentParser(description="Mini coding agent on Fireworks AI.")
    ap.add_argument("--model", help="model ID (else $FIREWORKS_MODEL)")
    ap.add_argument("--list-models", action="store_true", help="print available model IDs and exit")
    ap.add_argument("-p", "--prompt", help="run a single task non-interactively")
    ap.add_argument("--yolo", action="store_true", help="auto-approve all tool calls")
    ap.add_argument("--max-steps", type=int, default=30, help="max model calls per turn (default 30)")
    ap.add_argument("--cwd", default=os.getcwd(), help="working directory for tools (default: current)")
    args = ap.parse_args()

    key = os.environ.get("FIREWORKS_API_KEY")
    if not key:
        sys.exit("error: FIREWORKS_API_KEY is not set. Run: export FIREWORKS_API_KEY=...")
    from openai import OpenAI
    client = OpenAI(base_url=BASE_URL, api_key=key)

    if args.list_models:
        try:
            list_models(client)
        except Exception as e:
            sys.exit(f"error: {e}")
        return
    model = args.model or os.environ.get("FIREWORKS_MODEL") or DEFAULT_MODEL
    if not model:
        sys.exit("error: no model set. Run with --list-models, then pass --model or set FIREWORKS_MODEL.")
    CWD = os.path.abspath(os.path.expanduser(args.cwd))
    if not os.path.isdir(CWD):
        sys.exit(f"error: --cwd {CWD} is not a directory")
    YOLO = args.yolo

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    logpath = os.path.join(HERE, "logs", time.strftime("%Y%m%d-%H%M%S") + ".jsonl")

    def log(obj):
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    messages = [{"role": "system", "content": system_prompt()}]
    log(messages[0])

    def user_turn(text):
        m = {"role": "user", "content": text}
        messages.append(m); log(m)
        return run_turn(client, model, messages, log, args.max_steps)

    if args.prompt:
        user_turn(args.prompt)
        return
    print(f"{DIM}model {model} | cwd {CWD} | /reset, /exit{RESET}")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            print(); break
        except KeyboardInterrupt:
            print(); continue
        if not text:
            continue
        if text == "/exit":
            break
        if text == "/reset":
            del messages[1:]
            print("history cleared")
            continue
        user_turn(text)


if __name__ == "__main__":
    main()
