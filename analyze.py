"""Turn the raw runs in data/raw/ into the numbers in results/.

    python3 analyze.py             # reads every file in data/raw/, writes results/
    python3 analyze.py FILE [...]  # reads only these run files, writes out/<first file>/
    python3 analyze.py --check     # fails if results/ no longer matches the raw runs

Every number in results/ comes from this file. The README and the article quote the
numbers measured in the 20 runs, so anyone can check them from the raw logs without
calling the API again. It needs Python 3.9 or later and nothing else.

All the runs it reads must share one task and one model. It refuses a mix. A call that
hit an API error, or ended with no visible output, is left out of the per-call numbers.
Its run still counts, as a failed run.

What each call's numbers mean, all in seconds after that call was sent:

  first token    The first piece of visible output: text, or the start of a tool call.
                 Hidden thinking happens before it, so it sits inside this wait.
  last token     The last piece of visible output.
  writing speed  Visible tokens (output tokens minus reasoning tokens) divided by the
                 time from the first token to the last. Only calls with at least 20
                 visible tokens over at least 0.25 s count, because a one-line tool call
                 is too short to time.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "results"
# The bootstrap uses a fixed seed, so --check reproduces the same intervals.
SEED = 20261126
RESAMPLES = 5000
MIN_VISIBLE, MIN_SPAN = 20, 0.25
VISIBLE_ITEMS = ("function_call", "message")
FINAL_EVENTS = ("response.completed", "response.failed", "response.incomplete")


class RunFileError(Exception):
    """The run files cannot be read together."""


# ---------------------------------------------------------------- per call

def call_metrics(call: dict) -> dict | None:
    """Work out one model call's timings and token counts from its raw events.

    Returns None for a call that cannot be timed: one that hit an API error, which
    trace_agent.py writes with no usage, and one that ended with no visible output.
    """
    if "error" in call or "usage" not in call:
        return None
    usage = call["usage"]
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    out_tokens = usage.get("output_tokens", 0)
    visible = out_tokens - reasoning
    # Visible output starts with a delta, or with a tool call or message item. A reasoning
    # item does not count: it arrives in one piece once the thinking is done.
    events = call["events"]
    shown = [
        t for t, kind, item in events
        if kind == "response.output_item.added" and item in VISIBLE_ITEMS
    ]
    done = [
        t for t, kind, item in events
        if kind == "response.output_item.done" and item in VISIBLE_ITEMS
    ]
    delta_times = [d[0] for d in call["deltas"]]
    if not delta_times + shown:
        # A call that failed before any visible output has no first token to time.
        return None
    first = min(delta_times + shown)
    last = max(delta_times + done + [first])
    completed = next((t for t, kind, _ in events if kind in FINAL_EVENTS), last)
    span = last - first
    speed = visible / span if visible >= MIN_VISIBLE and span >= MIN_SPAN else None
    return {
        "step": call["step"], "sent_s": call["sent_s"], "first_token_s": first,
        "last_token_s": last, "completed_s": completed,
        "input_tokens": usage.get("input_tokens", 0), "cached_tokens": cached,
        "output_tokens": out_tokens, "reasoning_tokens": reasoning,
        "visible_tokens": visible, "writing_speed": speed, "tools": call["tool_calls"],
    }


# ---------------------------------------------------------------- small statistics

def median_ci(values: list, rng: random.Random) -> dict:
    """Median with a 95 percent bootstrap interval."""
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "lo": None, "hi": None}
    meds = sorted(
        statistics.median(rng.choices(values, k=len(values))) for _ in range(RESAMPLES)
    )
    return {
        "n": len(values), "median": round(statistics.median(values), 3),
        "lo": round(meds[int(0.025 * RESAMPLES)], 3),
        "hi": round(meds[int(0.975 * RESAMPLES) - 1], 3),
    }


def fit_line(xs: list, ys: list) -> dict | None:
    """Least-squares line y = a + b x. Returns a, b and how many points it used.

    Returns None when every x is the same, for example when no call had reasoning
    tokens, because then there is no line to fit.
    """
    n = len(xs)
    if n == 0:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return {"n": n, "intercept_s": round(my - b * mx, 4), "s_per_token": round(b, 6)}


# ---------------------------------------------------------------- per run

def run_metrics(run: dict) -> dict:
    calls = [call_metrics(c) for c in run["calls"]]
    calls = [c for c in calls if c is not None]
    tool_time = 0.0
    for step in {t["step"] for t in run["tools"]}:
        ts = [t for t in run["tools"] if t["step"] == step]
        tool_time += max(t["end_s"] for t in ts) - min(t["start_s"] for t in ts)
    first_tokens = sum(c["first_token_s"] for c in calls)
    writing = sum(c["last_token_s"] - c["first_token_s"] for c in calls)
    return {
        "source": run["source"], "run": run["run"], "effort": run["effort"],
        # A run with a call that could not be timed did not finish cleanly, so it fails.
        "success": run["success"] and len(calls) == len(run["calls"]),
        "total_s": run["total_s"], "calls": len(run["calls"]),
        "calls_left_out": len(run["calls"]) - len(calls), "cost_usd": run["cost_usd"],
        "reasoning_tokens": sum(c["reasoning_tokens"] for c in calls),
        "visible_tokens": sum(c["visible_tokens"] for c in calls),
        "waiting_for_first_token_s": round(first_tokens, 4),
        "writing_s": round(writing, 4), "tools_s": round(tool_time, 4),
        "call_metrics": calls, "tool_spans": run["tools"],
    }


def trace(run: dict, base_wait: float) -> dict:
    """Lay one run out as segments for the GIF: the wait, thinking, writing and tools.

    The thinking segment is an estimate. The API sends the reasoning item only once the
    thinking is over, so the stream gives no start time for it. A call with reasoning
    tokens is taken to have waited `base_wait` like any other call, plus its thinking.
    The thinking is the extra wait. `base_wait` is the intercept of the fit in
    thinking_fit(): the usual wait for the first token on a call with no reasoning tokens.
    With no fit, it is the median wait of the calls after the first.
    Dividing reasoning tokens by the writing speed would undercount it here, because
    thinking ran slower per token than writing.
    """
    steps = []
    for c in run["call_metrics"]:
        think = 0
        if c["reasoning_tokens"]:
            think = min(max(c["first_token_s"] - base_wait, 0), c["first_token_s"])
        steps.append({
            "step": c["step"], "sent_s": c["sent_s"], "first_token_s": c["first_token_s"],
            "reading_and_queue_s": round(c["first_token_s"] - think, 3),
            "thinking_s": round(think, 3),
            "writing_s": round(c["last_token_s"] - c["first_token_s"], 3),
            "done_s": round(c["completed_s"], 3),
            "reasoning_tokens": c["reasoning_tokens"],
            "visible_tokens": c["visible_tokens"], "cached_tokens": c["cached_tokens"],
            "input_tokens": c["input_tokens"], "tools": c["tools"],
            "tool_spans": [
                [t["name"], t["start_s"], t["end_s"]]
                for t in run["tool_spans"] if t["step"] == c["step"]
            ],
        })
    return {
        "run": run["run"], "effort": run["effort"], "total_s": run["total_s"],
        "steps": steps,
    }


# ---------------------------------------------------------------- report

def effort_summary(ms: list, rng: random.Random) -> dict:
    calls = [c for m in ms for c in m["call_metrics"]]
    later = [c for c in calls if c["step"] > 1]
    first_calls = [c["first_token_s"] for c in calls if c["step"] == 1]
    later_input = sum(c["input_tokens"] for c in later)
    task_time = sum(m["total_s"] for m in ms)
    shares = {
        k: sum(m[k] for m in ms) / task_time
        for k in ("waiting_for_first_token_s", "writing_s", "tools_s")
    }
    # The rest of the task time: the end of each stream after its last token, and the
    # script's own work between calls and tools.
    shares["other_s"] = 1 - sum(shares.values())
    return {
        "runs": len(ms), "succeeded": sum(m["success"] for m in ms),
        "task_time_s": median_ci([m["total_s"] for m in ms], rng),
        "model_calls": median_ci([m["calls"] for m in ms], rng),
        "first_token_s_per_call": median_ci([c["first_token_s"] for c in calls], rng),
        "first_token_s_first_call": median_ci(first_calls, rng),
        "writing_speed_tok_s": median_ci([c["writing_speed"] for c in calls], rng),
        "reasoning_tokens_per_run": median_ci([m["reasoning_tokens"] for m in ms], rng),
        "visible_tokens_per_run": median_ci([m["visible_tokens"] for m in ms], rng),
        "cache_hit_rate_after_first_call": (
            round(sum(c["cached_tokens"] for c in later) / later_input, 3)
            if later_input else None
        ),
        "share_of_task_time": {k: round(v, 3) for k, v in shares.items()},
        "cost_usd_per_run": round(statistics.median(m["cost_usd"] for m in ms), 5),
    }


def per_second(s_per_token: float | None) -> float | None:
    """Tokens a second from seconds a token, or None when that makes no sense."""
    if s_per_token is None or s_per_token <= 0:
        return None
    return round(1 / s_per_token, 1)


def thinking_fit(calls: list, rng: random.Random) -> dict | None:
    """Does thinking take about as long per token as writing?

    Fit the wait for the first token against reasoning tokens, over every call after the
    first. In the committed runs the first call was cached too (1484 of 1487 tokens), but
    its 2 tool calls arrived in one piece, so its wait includes their writing. It is also
    the first call on a new connection. Bootstrap the calls for an interval.

    Returns None when no call after the first had reasoning tokens.
    """
    pts = [(c["reasoning_tokens"], c["first_token_s"]) for c in calls if c["step"] > 1]
    fit = fit_line([p[0] for p in pts], [p[1] for p in pts])
    if fit is None:
        return None
    slopes = []
    for _ in range(RESAMPLES):
        sample = rng.choices(pts, k=len(pts))
        if len({x for x, _ in sample}) > 1:
            xs, ys = [x for x, _ in sample], [y for _, y in sample]
            slopes.append(fit_line(xs, ys)["s_per_token"])
    slopes.sort()
    lo = hi = None
    if slopes:
        lo, hi = slopes[int(0.025 * len(slopes))], slopes[int(0.975 * len(slopes)) - 1]
    fit.update({
        "calls_with_reasoning": sum(1 for x, _ in pts if x > 0),
        "s_per_token_lo": None if lo is None else round(lo, 6),
        "s_per_token_hi": None if hi is None else round(hi, 6),
        "implied_thinking_speed_tok_s": per_second(fit["s_per_token"]),
        "implied_speed_lo": per_second(hi), "implied_speed_hi": per_second(lo),
    })
    return fit


def display_path(f: Path) -> str:
    """The file's path from the repo root, or as given when it lies outside the repo."""
    try:
        return str(f.resolve().relative_to(ROOT))
    except ValueError:
        return str(f)


def load_runs(raw_files: list) -> list:
    """Read every run, and note the file it came from.

    Raises RunFileError when the files hold no runs, or runs of more than one task or
    model, because their numbers would mix.
    """
    runs = []
    for f in raw_files:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                run = json.loads(line)
            except json.JSONDecodeError:
                raise RunFileError(
                    f"{display_path(f)}, line {n}: not valid JSON. Was the run cut off?"
                ) from None
            runs.append(dict(run, source=display_path(f)))
    if not runs:
        raise RunFileError("The run files hold no runs.")
    for key in ("task", "model", "prices_per_mtok"):
        values = sorted({json.dumps(r.get(key, "not recorded")) for r in runs})
        if len(values) > 1:
            raise RunFileError(
                f"These runs mix more than one {key}: {', '.join(values)}. Pass the "
                "files for one task, one model and one set of prices."
            )
    return runs


def summarize(raw_files: list) -> tuple[dict, dict, str]:
    """Read the run files and return the summary, the traces and the summary as text."""
    runs = load_runs(raw_files)
    meta = {k: runs[0].get(k) for k in ("task", "model", "prices_per_mtok")}
    rng = random.Random(SEED)
    metrics = [run_metrics(r) for r in runs]
    if not any(m["call_metrics"] for m in metrics):
        raise RunFileError(
            "No call in these runs could be timed. Every call hit an API error or ended "
            "with no visible output. See the error field in the run file."
        )
    order = [m["effort"] for m in metrics]
    efforts = sorted(set(order), key=order.index)
    summary = {
        "source_files": [display_path(f) for f in raw_files], **meta,
        "runs": len(runs), "by_effort": {},
    }
    # The usual first-token wait with no thinking, for the traces. This point fit takes no
    # random draws, so it leaves the bootstrap intervals below unchanged.
    later = [c for m in metrics for c in m["call_metrics"] if c["step"] > 1]
    fit = fit_line(
        [c["reasoning_tokens"] for c in later], [c["first_token_s"] for c in later]
    )
    if fit is not None:
        base_wait = fit["intercept_s"]
    else:
        # No call after the first had reasoning tokens, so the usual wait is the median.
        base_wait = statistics.median([c["first_token_s"] for c in later] or [0.0])
    traces = {}
    for effort in efforts:
        ms = [m for m in metrics if m["effort"] == effort]
        summary["by_effort"][effort] = effort_summary(ms, rng)
        # The lower of the two middle runs by task time goes to the GIF. With an odd
        # number of runs it is the middle one.
        ordered = sorted(ms, key=lambda m: m["total_s"])
        traces[effort] = trace(ordered[(len(ordered) - 1) // 2], base_wait)
    if len(efforts) == 2:
        a, b = efforts
        # The 2 effort levels take turns, so a pair is the same run number in one file.
        pairs = {}
        for m in metrics:
            pairs.setdefault((m["source"], m["run"]), {})[m["effort"]] = m["total_s"]
        diffs = [p[b] - p[a] for p in pairs.values() if a in p and b in p]
        summary["paired_task_time_s"] = {
            "difference": f"{b} minus {a}", **median_ci(diffs, rng),
            "pairs_where_" + b + "_was_slower": sum(d > 0 for d in diffs),
        }
    all_calls = [c for m in metrics for c in m["call_metrics"]]
    visible = [c["visible_tokens"] for c in all_calls]
    summary["all_runs"] = {
        "model_calls_per_run": {
            "min": min(m["calls"] for m in metrics),
            "max": max(m["calls"] for m in metrics),
        },
        "visible_tokens_per_call": {
            "min": min(visible, default=None), "max": max(visible, default=None),
        },
        "calls_left_out": sum(m["calls_left_out"] for m in metrics),
        "cost_usd_total": round(sum(m["cost_usd"] for m in metrics), 5),
    }
    summary["first_token_vs_reasoning_tokens"] = thinking_fit(all_calls, rng)
    return summary, traces, render_md(summary)


def render_md(s: dict) -> str:
    def num(x, digits=None):
        """x rounded to `digits` places, or as it is when `digits` is None."""
        return f"{x:g}" if digits is None else f"{x:.{digits}f}"

    def ci(d, unit="", digits=None):
        if d["median"] is None:
            return "n/a"
        return (
            f"{num(d['median'], digits)}{unit} "
            f"({num(d['lo'], digits)} to {num(d['hi'], digits)})"
        )

    def percent(x, fmt=".0%"):
        return "n/a" if x is None else format(x, fmt)

    def pct(e, key, fmt=".0%"):
        return percent(e["share_of_task_time"][key], fmt)

    def speed(x):
        return "n/a" if x is None else num(x, 0)

    sources = ", ".join(f"`{f}`" for f in s["source_files"])
    lines = [
        f"# Results: {s['model']}, task `{s['task']}`, {s['runs']} runs",
        "",
        "Medians with 95 percent bootstrap intervals in brackets. The share rows and the "
        "cache hit rate are totals over all the runs. Seconds have 2 decimal places and "
        f"speeds are whole tokens a second. analyze.py wrote this file from {sources}. "
        "Do not edit it by hand.",
        "",
        "| | " + " | ".join(s["by_effort"]) + " |",
        "|---|" + "---|" * len(s["by_effort"]),
    ]
    rows = [
        ("runs that passed the tests", lambda e: f"{e['succeeded']} of {e['runs']}"),
        ("task time", lambda e: ci(e["task_time_s"], " s", 2)),
        ("model calls per task", lambda e: ci(e["model_calls"])),
        (
            "wait for first token, per call",
            lambda e: ci(e["first_token_s_per_call"], " s", 2),
        ),
        (
            "wait for first token, first call",
            lambda e: ci(e["first_token_s_first_call"], " s", 2),
        ),
        ("writing speed", lambda e: ci(e["writing_speed_tok_s"], " tokens/s", 0)),
        ("reasoning tokens per task", lambda e: ci(e["reasoning_tokens_per_run"])),
        ("visible tokens per task", lambda e: ci(e["visible_tokens_per_run"])),
        (
            "cache hit rate after the first call",
            lambda e: percent(e["cache_hit_rate_after_first_call"]),
        ),
        ("time waiting for first tokens", lambda e: pct(e, "waiting_for_first_token_s")),
        ("time writing", lambda e: pct(e, "writing_s")),
        ("time in tools", lambda e: pct(e, "tools_s", ".1%")),
        (
            "other time: the end of each stream and the script",
            lambda e: pct(e, "other_s", ".1%"),
        ),
        ("cost per task (estimate)", lambda e: f"${e['cost_usd_per_run']:.4f}"),
    ]
    for label, fn in rows:
        cells = " | ".join(fn(e) for e in s["by_effort"].values())
        lines.append(f"| {label} | {cells} |")
    if "paired_task_time_s" in s:
        p = s["paired_task_time_s"]
        slower = [k for k in p if k.startswith("pairs_where")][0]
        lines += [
            "", f"Paired by run number in the same file, {p['difference']}: "
            f"{ci(p, ' s', 2)}. {p[slower]} of {p['n']} pairs were slower at the higher "
            "effort.",
        ]
    a = s["all_runs"]
    calls, visible = a["model_calls_per_run"], a["visible_tokens_per_call"]
    lines += [
        "", f"Over all {s['runs']} runs, a task made {calls['min']} to {calls['max']} "
        f"model calls, a call wrote {visible['min']} to {visible['max']} visible tokens, "
        f"and the estimated cost came to ${a['cost_usd_total']:.4f} in all.",
    ]
    if a["calls_left_out"]:
        lines += [
            "", "Calls left out of the per-call numbers because of an API error or no "
            f"visible output: {a['calls_left_out']}. Their runs count as failed.",
        ]
    f = s["first_token_vs_reasoning_tokens"]
    if f is None:
        lines += [
            "", "No calls after the first had reasoning tokens. Hence there is no "
            "thinking fit.",
        ]
    else:
        lines += [
            "", f"Wait for the first token against reasoning tokens, {f['n']} calls "
            f"after the first: {num(f['intercept_s'], 2)} s plus "
            f"{f['s_per_token'] * 1000:.2f} ms per reasoning token. The thinking speed "
            f"comes out at about {speed(f['implied_thinking_speed_tok_s'])} reasoning "
            f"tokens a second ({speed(f['implied_speed_lo'])} to "
            f"{speed(f['implied_speed_hi'])}). "
            f"Only {f['calls_with_reasoning']} of those calls had any reasoning tokens.",
        ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "files", nargs="*", type=Path,
        help="run files to read, all for one task and one model (default: every "
        ".jsonl file in data/raw/)"
    )
    ap.add_argument(
        "--check", action="store_true",
        help="exit 1 if results/ no longer matches data/raw/"
    )
    args = ap.parse_args(argv)
    raw_files = args.files or sorted(RAW.glob("*.jsonl"))
    if not raw_files:
        print(
            "No run files in data/raw/. To read your own runs, pass their files, such as "
            "out/runs/<your file>.", file=sys.stderr
        )
        return 2
    missing = [str(f) for f in raw_files if not f.is_file()]
    if missing:
        print("No such file: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        summary, traces, md = summarize(raw_files)
    except RunFileError as err:
        print(err, file=sys.stderr)
        return 2
    outputs = {"summary.json": json.dumps(summary, indent=1) + "\n", "summary.md": md}
    for effort, t in traces.items():
        outputs[f"trace-{effort}.json"] = json.dumps(t, indent=1) + "\n"
    # Your own run files get their own folder, so the committed results/ stay as they are.
    out = OUT if not args.files else ROOT / "out" / args.files[0].stem
    where = display_path(out) + "/"
    if args.check:
        stale = [
            name for name, text in outputs.items()
            if not (out / name).exists() or (out / name).read_text() != text
        ]
        if stale:
            print(f"{where} is out of date: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"{where} matches the raw runs.")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (out / name).write_text(text)
    print(md)
    print(f"Wrote {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
