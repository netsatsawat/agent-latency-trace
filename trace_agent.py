"""Time a small coding agent, one model call at a time, on OpenAI's Responses API.

The agent gets a tiny project with failing tests (tasks/<name>/). It can read a file,
edit a file and run the tests. For every model call this script streams the response and
writes down when each event arrives. From that you can see how long the model took
before its first visible token, how many tokens it spent thinking and writing, how much
of the prompt came from the cache, and how long each tool ran.

    export OPENAI_API_KEY=...
    python3 trace_agent.py --task checkout --efforts medium xhigh --runs 10 --max-cost 5

It needs Python 3.9 or later and nothing outside the standard library. Each run starts
from a fresh copy of the task folder. The tests run code the model wrote. On macOS they
run under sandbox-exec, which blocks the network and stops writes outside the run's own
folder. On other systems the script stops unless you pass --no-sandbox. Pass it only
inside a container or a virtual machine you can delete afterwards. On every system, the
tests get only PATH, HOME, LANG and TMPDIR from the environment. They can still read files.

Every run is one line in out/runs/<date>-<time>-<model>-<task>.jsonl, with the date and
time in UTC. Each use of the script writes a new file there, and git ignores out/. To
see the numbers for your runs, pass that file to analyze.py. Our 20 runs in data/raw/
and the numbers in results/ stay as they are.
"""
from __future__ import annotations

import argparse
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
# A fixed key keeps this experiment's cache use apart from other calls. According to
# OpenAI's prompt caching guide, GPT-5.6 and later models do not need it for cache hits.
CACHE_KEY = "agent-latency-trace"
# According to OpenAI's error-codes guide, a retry does not fix these 429 errors. Their
# error type can also be insufficient_quota. The script stops the batch on any of them.
BILLING_429 = (
    "insufficient_quota", "credit_balance_exhausted",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
)
# A run that needs more model calls than this stops and counts as failed.
MAX_CALLS = 10
TEST_TIMEOUT_S = 60

# Prices in US dollars per million tokens, for the spending cap and each run's cost
# estimate. The defaults are gpt-6.2-sol's prices from OpenAI's pricing page on
# 2026-11-26. For uncached input we use its 30-minute cache-write price of $2.50, which is
# above its $2.00 input price, so the running total comes out a little high, never low.
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


def run_tests(workdir: Path, sandbox: bool = True) -> tuple[bool, str]:
    """Run the project's tests. Return whether they passed and the last lines printed.

    With `sandbox`, the tests run under sandbox-exec. main() checks that it exists first.
    """
    cmd = [sys.executable, "-B", "-m", "unittest", "-q"]
    if sandbox:
        # No network, and no writes anywhere except this run's folder and the null
        # device.
        profile = (
            "(version 1)(allow default)(deny network*)(deny file-write*)"
            f'(allow file-write* (subpath "{workdir.resolve()}") (literal "/dev/null"))'
        )
        cmd = ["sandbox-exec", "-p", profile] + cmd
    # The tests run code the model wrote, and their output goes back to the model. We pass
    # them only these 4 variables, so they cannot see a key or token in the environment.
    env = {
        k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ
    }
    try:
        done = subprocess.run(
            cmd, cwd=workdir, env=env, capture_output=True, text=True,
            timeout=TEST_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return False, f"The tests did not finish within {TEST_TIMEOUT_S} seconds."
    text = (done.stdout + done.stderr).strip()
    return done.returncode == 0, "\n".join(text.splitlines()[-30:])


def run_tool(workdir: Path, name: str, arguments: str, sandbox: bool = True) -> str:
    """Run one tool call and return its result as text, errors included.

    `arguments` is the JSON text the model wrote. Bad JSON comes back as a tool error.
    """
    try:
        args = json.loads(arguments)
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
            return run_tests(workdir, sandbox)[1]
        return f"There is no tool called {name}."
    except (OSError, ValueError, KeyError, TypeError) as err:
        return f"Tool error: {err}"


# ---------------------------------------------------------------- one model call

class APIError(RuntimeError):
    """The API answered with an HTTP error. `status` holds the HTTP status code."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def stream_call(conn: http.client.HTTPSConnection, payload: dict, api_key: str) -> dict:
    """Send one streamed request. Return its event timings, output items, token usage
    and 2 response headers.

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
        raise APIError(resp.status, detail)
    # According to OpenAI's API reference, x-request-id is the request's unique ID for
    # troubleshooting, and openai-processing-ms is the time OpenAI took to process the
    # request. We keep both as text, as the API sent them.
    ids = {
        "request_id": resp.getheader("x-request-id"),
        "processing_ms": resp.getheader("openai-processing-ms"),
    }
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
        **ids,
    }


def script_commit() -> str | None:
    """The short hash of the git commit this script runs from, or None outside git."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def call_cost(usage: dict, prices: dict) -> float:
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    fresh = usage.get("input_tokens", 0) - cached
    return (
        fresh * prices["input"] + cached * prices["cached"]
        + usage.get("output_tokens", 0) * prices["output"]
    ) / 1e6


# ---------------------------------------------------------------- one agent run

def run_agent(
    task_name: str, model: str, effort: str, api_key: str, prices: dict,
    spend_left: float, sandbox: bool = True
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
                "http_status": getattr(err, "status", None),
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
        if call["status"] != "completed":
            # A failed or cut-off response is an API error, not an answer.
            stop = "api_error"
            break
        fn_calls = [o for o in output if o.get("type") == "function_call"]
        if not fn_calls:
            stop = "answered"
            break

        # We run the tool calls one at a time, in the order the model asked for them.
        # When we ran 2 edits to one file at the same time, one edit often overwrote the
        # other.
        for fc in fn_calls:
            start = time.perf_counter() - t_run
            result = run_tool(workdir, fc["name"], fc.get("arguments") or "{}", sandbox)
            end = time.perf_counter() - t_run
            tools.append({
                "step": step, "name": fc["name"],
                "start_s": round(start, 4), "end_s": round(end, 4),
            })
            items.append({
                "type": "function_call_output", "call_id": fc["call_id"],
                "output": result,
            })

    total = round(time.perf_counter() - t_run, 4)
    conn.close()
    # Run the tests again to decide success. The model's reply does not count.
    passed, _ = run_tests(workdir, sandbox)
    shutil.rmtree(workdir, ignore_errors=True)
    return {
        "task": task_name, "model": model, "effort": effort, "stop": stop,
        "success": passed and stop == "answered", "total_s": total,
        "model_calls": len(calls), "cost_usd": round(cost, 5),
        "calls": calls, "tools": tools,
    }


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    # Folders only. A stray file such as .DS_Store is not a task.
    tasks = sorted(p.name for p in (ROOT / "tasks").iterdir() if p.is_dir())
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  python3 trace_agent.py --task checkout --model gpt-6.2-sol "
        "--efforts medium xhigh --runs 10 --max-cost 5",
    )
    ap.add_argument(
        "--task", default="checkout", choices=tasks,
        help="the project in tasks/ that the agent fixes (default: checkout)"
    )
    ap.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"OpenAI model name (default: {DEFAULT_MODEL}). For any other model, pass "
        "all 3 --price flags"
    )
    ap.add_argument(
        "--efforts", nargs="+", default=["medium", "xhigh"],
        help="reasoning effort levels to compare. The runs take turns between them "
        "(default: medium xhigh)"
    )
    ap.add_argument(
        "--runs", type=int, default=10, help="runs per effort level (default: 10)"
    )
    ap.add_argument(
        "--max-cost", type=float, default=5.0,
        help="spending cap in US dollars for all the runs together, checked before each "
        "model call (default: 5)"
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
    ap.add_argument(
        "--out", type=Path, default=None,
        help="JSONL file to append runs to (default: a new file in out/runs/)"
    )
    ap.add_argument(
        "--no-sandbox", action="store_true",
        help="run the tests without sandbox-exec. Use it only inside a container or "
        "virtual machine you can delete"
    )
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be 1 or more")
    if args.max_cost <= 0:
        ap.error("--max-cost must be above 0")

    given = {
        "input": args.price_input, "cached": args.price_cached,
        "output": args.price_output,
    }
    # A model with only some prices given would use Sol's prices for the rest.
    if args.model != DEFAULT_MODEL and any(v is None for v in given.values()):
        print(
            f"The default prices are for {DEFAULT_MODEL}. For {args.model}, pass all 3 "
            "of --price-input, --price-cached and --price-output, in US dollars per "
            "million tokens.", file=sys.stderr
        )
        return 2
    prices = {k: DEFAULT_PRICES[k] if v is None else v for k, v in given.items()}
    if not shutil.which("sandbox-exec") and not args.no_sandbox:
        print(
            "sandbox-exec is not on this system. Without it, the tests would run the "
            "model's code with no sandbox. Run the script inside a container or virtual "
            "machine you can delete, and pass --no-sandbox.", file=sys.stderr
        )
        return 2
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY first.", file=sys.stderr)
        return 2
    # Each use of the script gets a new file. A second batch never appends to the first.
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = args.out or ROOT / "out" / "runs" / f"{stamp}-{args.model}-{args.task}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Alternate the effort levels run by run, so a slow period on the API affects both
    # effort levels equally.
    plan = [(i, e) for i in range(1, args.runs + 1) for e in args.efforts]
    commit = script_commit()
    spent = 0.0
    for i, effort in plan:
        if spent >= args.max_cost:
            print(f"Stopped at the spending cap of ${args.max_cost:.2f}.")
            break
        started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        left = args.max_cost - spent
        run = run_agent(
            args.task, args.model, effort, api_key, prices, left, not args.no_sandbox
        )
        spent += run["cost_usd"]
        run.update({
            "run": i, "started_utc": started, "prices_per_mtok": prices,
            "python": platform.python_version(), "os": platform.system(),
            "script_commit": commit,
        })
        with out.open("a") as fh:
            fh.write(json.dumps(run) + "\n")
        print(
            f"run {i:>2} {effort:<6} {run['stop']:<12} ok={run['success']!s:<5} "
            f"calls={run['model_calls']} {run['total_s']:6.1f} s  ${run['cost_usd']:.4f}"
            f"  (total ${spent:.3f})"
        )
        if run["stop"] == "api_error":
            last = run["calls"][-1]
            reason = last.get("error", f"The response ended as {last.get('status')}.")
            print("  " + reason[:300], file=sys.stderr)
            no_credit = (
                last.get("http_status") == 429 and any(c in reason for c in BILLING_429)
            )
            if last.get("http_status") in (400, 401, 403, 404) or no_credit:
                # A bad key, a bad model name or an account with no credit fails every
                # run in the same way.
                print(
                    "This error would repeat on every run. The script stops here. "
                    f"Wrote {out}.", file=sys.stderr
                )
                return 1
    print(f"Wrote {out}. Spent about ${spent:.3f}.")
    print(f"To see the numbers for these runs alone: python3 analyze.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
