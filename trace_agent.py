"""Time a small coding agent, one model call at a time, on OpenAI's Responses API.

The agent gets a tiny project with failing tests (tasks/<name>/). It can read a file,
edit a file and run the tests. For every model call this script streams the response and
writes down when each event arrives. From that you can see how long the model took
before its first visible token, how many tokens it spent thinking and writing, how much
of the prompt came from the cache, and how long each tool ran.

    export OPENAI_API_KEY=...
    python3 trace_agent.py --task checkout --efforts medium xhigh --runs 10 --max-cost 5

It needs Python 3.9 or later and nothing outside the standard library. Each run starts
from a fresh copy of the task folder. On macOS the tests run under sandbox-exec, which
blocks the network and stops writes outside the run's own folder, because the code under
test is code the model wrote. Every run is one line in
data/raw/<date>-<time>-<model>-<task>.jsonl, with the date and time in UTC, so each use
of the script writes a new file. analyze.py turns those lines into the numbers in
results/.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_HOST, API_PATH = "api.openai.com", "/v1/responses"
# The same key on every call routes the calls to the same cache.
CACHE_KEY = "agent-latency-trace"
# A run that needs more model calls than this stops and counts as failed.
MAX_CALLS = 10
TEST_TIMEOUT_S = 60

# Prices in US dollars per million tokens, for the spending cap and each run's cost
# estimate. The defaults are gpt-6.2-sol's standard rates from OpenAI's model page on
# 2026-11-26. Uncached input is charged at the cache-write rate, the higher of the two,
# so the running total comes out a little high, never low.
DEFAULT_MODEL = "gpt-6.2-sol"
DEFAULT_PRICES = {"input": 2.50, "cached": 0.10, "output": 10.00}

FILE_NAME = {"type": "string", "description": "A file name, such as prices.py."}
TOOLS = [
    {
        "type": "function", "name": "read_file", "strict": True,
        "description": "Return the full text of one file in the project folder.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["path"],
            "properties": {"path": FILE_NAME},
        },
    },
    {
        "type": "function", "name": "edit_file", "strict": True,
        "description": "Replace one exact piece of text in a file with new text.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "old_text", "new_text"],
            "properties": {
                "path": FILE_NAME,
                "old_text": {
                    "type": "string",
                    "description": "Text that appears exactly once in the file.",
                },
                "new_text": {
                    "type": "string", "description": "The text to put in its place.",
                },
            },
        },
    },
    {
        "type": "function", "name": "run_tests", "strict": True,
        "description": (
            "Run every unittest test in the project and return what the runner printed."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": [],
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------- tools

def _inside(workdir: Path, name: str) -> Path:
    path = (workdir / name).resolve()
    if path.parent != workdir.resolve():
        raise ValueError(f"{name} is not a file in the project folder")
    return path


def run_tests(workdir: Path) -> tuple[bool, str]:
    """Run the project's tests. Return whether they passed and the last lines printed."""
    cmd = [sys.executable, "-B", "-m", "unittest", "-q"]
    if shutil.which("sandbox-exec"):
        # No network, and no writes anywhere except this run's folder and the null
        # device.
        profile = (
            "(version 1)(allow default)(deny network*)(deny file-write*)"
            f'(allow file-write* (subpath "{workdir.resolve()}") (literal "/dev/null"))'
        )
        cmd = ["sandbox-exec", "-p", profile] + cmd
    try:
        done = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=TEST_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return False, f"The tests did not finish within {TEST_TIMEOUT_S} seconds."
    text = (done.stdout + done.stderr).strip()
    return done.returncode == 0, "\n".join(text.splitlines()[-30:])


def run_tool(workdir: Path, name: str, args: dict) -> str:
    """Run one tool call and return its result as text, errors included."""
    try:
        if name == "read_file":
            return _inside(workdir, args["path"]).read_text()
        if name == "edit_file":
            path = _inside(workdir, args["path"])
            text = path.read_text()
            count = text.count(args["old_text"])
            if count != 1:
                return (
                    f"Edit failed: the old text appears {count} times in {args['path']}, "
                    "not once."
                )
            path.write_text(text.replace(args["old_text"], args["new_text"]))
            return f"Edited {args['path']}."
        if name == "run_tests":
            return run_tests(workdir)[1]
        return f"There is no tool called {name}."
    except (OSError, ValueError, KeyError) as err:
        return f"Tool error: {err}"


# ---------------------------------------------------------------- one model call

def stream_call(conn: http.client.HTTPSConnection, payload: dict, api_key: str) -> dict:
    """Send one streamed request. Return its event timings, output items and token
    usage.

    Times are seconds after the request was sent. Each event is kept as [time, type,
    item type]. Text and argument deltas are kept as [time, output index, characters]
    instead, so the file records when tokens arrived but none of what they said.
    """
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    events, deltas, final = [], [], None
    t0 = time.perf_counter()
    conn.request("POST", API_PATH, body=body, headers=headers)
    resp = conn.getresponse()
    if resp.status != 200:
        detail = resp.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {resp.status}: {detail}")
    data_lines = []
    while True:
        raw = resp.readline()
        if not raw:   # the stream has ended and the connection is free for the next call
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue
        if line or not data_lines:
            continue
        t = round(time.perf_counter() - t0, 4)
        msg = json.loads("\n".join(data_lines))
        data_lines = []
        kind = msg.get("type", "")
        if kind.endswith(".delta"):
            deltas.append([t, msg.get("output_index", -1), len(msg.get("delta", ""))])
            continue
        item_type = (msg.get("item") or {}).get("type", "")
        events.append([t, kind, item_type])
        if kind in ("response.completed", "response.failed", "response.incomplete"):
            final = msg["response"]
        if kind == "error":
            raise RuntimeError(f"stream error: {msg}")
    if final is None:
        raise RuntimeError("the stream ended without a final response")
    return {
        "events": events, "deltas": deltas, "status": final.get("status"),
        "usage": final.get("usage") or {}, "output": final.get("output") or [],
    }


def call_cost(usage: dict, prices: dict) -> float:
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    fresh = usage.get("input_tokens", 0) - cached
    return (
        fresh * prices["input"] + cached * prices["cached"]
        + usage.get("output_tokens", 0) * prices["output"]
    ) / 1e6


# ---------------------------------------------------------------- one agent run

def run_agent(
    task_name: str, model: str, effort: str, api_key: str, prices: dict, spend_left: float
) -> dict:
    """Run the agent once on a fresh copy of the task and return what was timed."""
    system = (ROOT / "prompts" / "system.md").read_text()
    task = (ROOT / "prompts" / f"task-{task_name}.md").read_text()
    workdir = Path(tempfile.mkdtemp(prefix="agent-latency-"))
    for f in (ROOT / "tasks" / task_name).glob("*.py"):
        shutil.copy(f, workdir / f.name)

    # The system prompt goes first in the input, as a developer message. On GPT-6.2
    # models the top-level `instructions` field sits after the input, past the part the
    # cache can cover, so a system prompt sent there is never cached (OpenAI's prompt
    # caching guide, read 2026-11-26). Our first pilot run sent it there and got no
    # cache hits at all.
    items = [{"role": "developer", "content": system}, {"role": "user", "content": task}]
    calls, tools, cost, stop = [], [], 0.0, "max_calls"
    # One connection per run, reused by every call in it, as an SDK client would do.
    conn = http.client.HTTPSConnection(API_HOST, timeout=600)
    t_run = time.perf_counter()
    for step in range(1, MAX_CALLS + 1):
        if cost > spend_left:
            stop = "spending_cap"
            break
        payload = {
            "model": model, "input": items, "tools": TOOLS, "prompt_cache_key": CACHE_KEY,
            "reasoning": {"effort": effort}, "parallel_tool_calls": True,
            "store": False, "include": ["reasoning.encrypted_content"], "stream": True,
        }
        sent = time.perf_counter() - t_run
        try:
            call = stream_call(conn, payload, api_key)
        except (OSError, http.client.HTTPException, RuntimeError, ValueError) as err:
            calls.append({
                "step": step, "sent_s": round(sent, 4), "error": str(err)[:500],
            })
            stop = "api_error"
            break
        cost += call_cost(call["usage"], prices)
        output = call.pop("output")
        call.update({
            "step": step, "sent_s": round(sent, 4),
            "output_types": [o.get("type") for o in output],
            "tool_calls": [
                o.get("name") for o in output if o.get("type") == "function_call"
            ],
        })
        calls.append(call)
        items.extend(output)
        fn_calls = [o for o in output if o.get("type") == "function_call"]
        if not fn_calls:
            stop = "answered"
            break

        # Tool calls from one step run at the same time, because they cannot depend on
        # each other: the model asked for them together.
        def timed(fc):
            start = time.perf_counter() - t_run
            args = json.loads(fc.get("arguments") or "{}")
            result = run_tool(workdir, fc["name"], args)
            end = time.perf_counter() - t_run
            return fc, result, round(start, 4), round(end, 4)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(fn_calls)) as pool:
            for fc, result, start, end in pool.map(timed, fn_calls):
                tools.append({
                    "step": step, "name": fc["name"], "start_s": start, "end_s": end,
                })
                items.append({
                    "type": "function_call_output", "call_id": fc["call_id"],
                    "output": result,
                })

    total = round(time.perf_counter() - t_run, 4)
    conn.close()
    # Run the tests again to decide success. The model's reply does not count.
    passed, _ = run_tests(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    return {
        "task": task_name, "model": model, "effort": effort, "stop": stop,
        "success": passed and stop == "answered", "total_s": total,
        "model_calls": len(calls), "cost_usd": round(cost, 5),
        "calls": calls, "tools": tools,
    }


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    tasks = sorted(p.name for p in (ROOT / "tasks").iterdir())
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", default="checkout", choices=tasks)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--efforts", nargs="+", default=["medium", "xhigh"])
    ap.add_argument("--runs", type=int, default=10, help="runs per effort level")
    ap.add_argument(
        "--max-cost", type=float, default=5.0,
        help="spending cap in US dollars, checked before each model call"
    )
    ap.add_argument(
        "--price-input", type=float, help="US dollars per million uncached input tokens"
    )
    ap.add_argument(
        "--price-cached", type=float, help="US dollars per million cached input tokens"
    )
    ap.add_argument(
        "--price-output", type=float, help="US dollars per million output tokens"
    )
    ap.add_argument("--out", type=Path, default=None, help="JSONL file to append runs to")
    args = ap.parse_args(argv)

    given = {
        "input": args.price_input, "cached": args.price_cached,
        "output": args.price_output,
    }
    if args.model != DEFAULT_MODEL and all(v is None for v in given.values()):
        print(
            f"The default prices are for {DEFAULT_MODEL}. For {args.model}, pass "
            "--price-input, --price-cached and --price-output, in US dollars per "
            "million tokens.", file=sys.stderr
        )
        return 2
    prices = {k: DEFAULT_PRICES[k] if v is None else v for k, v in given.items()}
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY first.", file=sys.stderr)
        return 2
    # A new file for each use of the script, so a second batch never appends to the first.
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = args.out or ROOT / "data" / "raw" / f"{stamp}-{args.model}-{args.task}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Alternate the effort levels run by run, so a slow period on the API affects both
    # effort levels equally.
    plan = [(i, e) for i in range(1, args.runs + 1) for e in args.efforts]
    spent = 0.0
    for i, effort in plan:
        if spent >= args.max_cost:
            print(f"Stopped at the spending cap of ${args.max_cost:.2f}.")
            break
        started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        left = args.max_cost - spent
        run = run_agent(args.task, args.model, effort, api_key, prices, left)
        spent += run["cost_usd"]
        run.update({
            "run": i, "started_utc": started, "prices_per_mtok": prices,
            "python": platform.python_version(), "os": platform.system(),
        })
        with out.open("a") as fh:
            fh.write(json.dumps(run) + "\n")
        print(
            f"run {i:>2} {effort:<6} {run['stop']:<12} ok={run['success']!s:<5} "
            f"calls={run['model_calls']} {run['total_s']:6.1f} s  ${run['cost_usd']:.4f}"
            f"  (total ${spent:.3f})"
        )
    print(f"Wrote {out}. Spent about ${spent:.3f}.")
    print(f"To see the numbers for these runs alone: python3 analyze.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
