"""Turn the raw runs in data/raw/ into the numbers in results/.

    python3 analyze.py           # writes summary.json, summary.md and trace-*.json
    python3 analyze.py --check   # fails if results/ no longer matches the raw runs

Every number in the README and the article comes from this file, so anyone can check it
from the raw logs without calling the API again. It needs Python 3.9 or later and
nothing else.

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


# ---------------------------------------------------------------- per call

def call_metrics(call: dict) -> dict:
    """Work out one model call's timings and token counts from its raw events."""
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
    first = min(delta_times + shown)
    last = max(delta_times + done)
    created = next(t for t, kind, _ in events if kind == "response.created")
    completed = next(t for t, kind, _ in events if kind == "response.completed")
    span = last - first
    speed = visible / span if visible >= MIN_VISIBLE and span >= MIN_SPAN else None
    return {
        "step": call["step"], "sent_s": call["sent_s"], "created_s": created,
        "first_token_s": first, "last_token_s": last, "completed_s": completed,
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


def fit_line(xs: list, ys: list) -> dict:
    """Least-squares line y = a + b x. Returns a, b and how many points it used."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return {"n": n, "intercept_s": round(my - b * mx, 4), "s_per_token": round(b, 6)}


# ---------------------------------------------------------------- per run

def run_metrics(run: dict) -> dict:
    calls = [call_metrics(c) for c in run["calls"]]
    tool_time = 0.0
    for step in {t["step"] for t in run["tools"]}:
        ts = [t for t in run["tools"] if t["step"] == step]
        tool_time += max(t["end_s"] for t in ts) - min(t["start_s"] for t in ts)
    first_tokens = sum(c["first_token_s"] for c in calls)
    writing = sum(c["last_token_s"] - c["first_token_s"] for c in calls)
    return {
        "run": run["run"], "effort": run["effort"], "success": run["success"],
        "total_s": run["total_s"], "calls": len(calls), "cost_usd": run["cost_usd"],
        "reasoning_tokens": sum(c["reasoning_tokens"] for c in calls),
        "visible_tokens": sum(c["visible_tokens"] for c in calls),
        "waiting_for_first_token_s": round(first_tokens, 4),
        "writing_s": round(writing, 4), "tools_s": round(tool_time, 4),
        "call_metrics": calls, "tool_spans": run["tools"],
    }


def trace(run: dict, speed: float) -> dict:
    """Lay one run out as segments for the GIF: the wait, thinking, writing and tools.

    The thinking segment is an estimate: reasoning tokens divided by the writing speed,
    the method the article describes. The API sends the reasoning item only once the
    thinking is over, so the stream gives no start time for it.
    """
    steps = []
    for c in run["call_metrics"]:
        think = 0
        if c["reasoning_tokens"]:
            think = min(c["reasoning_tokens"] / speed, c["first_token_s"])
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
    return {
        "runs": len(ms), "succeeded": sum(m["success"] for m in ms),
        "task_time_s": median_ci([m["total_s"] for m in ms], rng),
        "model_calls": median_ci([m["calls"] for m in ms], rng),
        "first_token_s_per_call": median_ci([c["first_token_s"] for c in calls], rng),
        "first_token_s_first_call": median_ci(first_calls, rng),
        "writing_speed_tok_s": median_ci([c["writing_speed"] for c in calls], rng),
        "reasoning_tokens_per_run": median_ci([m["reasoning_tokens"] for m in ms], rng),
        "visible_tokens_per_run": median_ci([m["visible_tokens"] for m in ms], rng),
        "cache_hit_rate_after_first_call": round(
            sum(c["cached_tokens"] for c in later)
            / sum(c["input_tokens"] for c in later), 3
        ),
        "share_of_task_time": {
            k: round(sum(m[k] for m in ms) / sum(m["total_s"] for m in ms), 3)
            for k in ("waiting_for_first_token_s", "writing_s", "tools_s")
        },
        "cost_usd_per_run": round(statistics.median(m["cost_usd"] for m in ms), 5),
    }


def thinking_fit(calls: list, rng: random.Random) -> dict:
    """Does thinking take about as long per token as writing?

    Fit the wait for the first token against reasoning tokens, over every call after the
    first, where the prompt is mostly cached. Bootstrap the calls for an interval.
    """
    pts = [(c["reasoning_tokens"], c["first_token_s"]) for c in calls if c["step"] > 1]
    fit = fit_line([p[0] for p in pts], [p[1] for p in pts])
    slopes = []
    for _ in range(RESAMPLES):
        sample = rng.choices(pts, k=len(pts))
        if len({x for x, _ in sample}) > 1:
            xs, ys = [x for x, _ in sample], [y for _, y in sample]
            slopes.append(fit_line(xs, ys)["s_per_token"])
    slopes.sort()
    lo, hi = slopes[int(0.025 * len(slopes))], slopes[int(0.975 * len(slopes)) - 1]
    fit.update({
        "calls_with_reasoning": sum(1 for x, _ in pts if x > 0),
        "s_per_token_lo": round(lo, 6), "s_per_token_hi": round(hi, 6),
        "implied_thinking_speed_tok_s": round(1 / fit["s_per_token"], 1),
        "implied_speed_lo": round(1 / hi, 1), "implied_speed_hi": round(1 / lo, 1),
    })
    return fit


def analyse(raw_files: list) -> tuple[dict, dict, str]:
    runs = [
        json.loads(line) for f in raw_files for line in f.read_text().splitlines() if line
    ]
    meta = {k: runs[0][k] for k in ("task", "model", "prices_per_mtok")}
    rng = random.Random(SEED)
    metrics = [run_metrics(r) for r in runs]
    order = [m["effort"] for m in metrics]
    efforts = sorted(set(order), key=order.index)
    summary = {
        "source_files": [f.name for f in raw_files], **meta, "runs": len(runs),
        "by_effort": {},
    }
    traces = {}
    for effort in efforts:
        ms = [m for m in metrics if m["effort"] == effort]
        summary["by_effort"][effort] = effort_summary(ms, rng)
        # The run with the median task time goes to the GIF. With an even number of runs
        # it is the lower of the two middle ones.
        ordered = sorted(ms, key=lambda m: m["total_s"])
        speed = summary["by_effort"][effort]["writing_speed_tok_s"]["median"]
        traces[effort] = trace(ordered[(len(ordered) - 1) // 2], speed)
    if len(efforts) == 2:
        a, b = efforts
        pairs = {}
        for m in metrics:
            pairs.setdefault(m["run"], {})[m["effort"]] = m["total_s"]
        diffs = [p[b] - p[a] for p in pairs.values() if a in p and b in p]
        summary["paired_task_time_s"] = {
            "difference": f"{b} minus {a}", **median_ci(diffs, rng),
            "pairs_where_" + b + "_was_slower": sum(d > 0 for d in diffs),
        }
    all_calls = [c for m in metrics for c in m["call_metrics"]]
    summary["first_token_vs_reasoning_tokens"] = thinking_fit(all_calls, rng)
    return summary, traces, render_md(summary)


def render_md(s: dict) -> str:
    def ci(d, unit=""):
        if d["median"] is None:
            return "n/a"
        return f"{d['median']:g}{unit} ({d['lo']:g} to {d['hi']:g})"

    def pct(e, key, fmt=".0%"):
        return format(e["share_of_task_time"][key], fmt)

    sources = ", ".join(f"`data/raw/{f}`" for f in s["source_files"])
    lines = [
        f"# Results: {s['model']}, task `{s['task']}`, {s['runs']} runs",
        "",
        "Medians with 95 percent bootstrap intervals in brackets. Generated by "
        f"analyze.py from {sources}. Do not edit by hand.",
        "",
        "| | " + " | ".join(s["by_effort"]) + " |",
        "|---|" + "---|" * len(s["by_effort"]),
    ]
    rows = [
        ("runs that passed the tests", lambda e: f"{e['succeeded']} of {e['runs']}"),
        ("task time", lambda e: ci(e["task_time_s"], " s")),
        ("model calls per task", lambda e: ci(e["model_calls"])),
        (
            "wait for first token, per call",
            lambda e: ci(e["first_token_s_per_call"], " s"),
        ),
        (
            "wait for first token, first call",
            lambda e: ci(e["first_token_s_first_call"], " s"),
        ),
        ("writing speed", lambda e: ci(e["writing_speed_tok_s"], " tokens/s")),
        ("reasoning tokens per task", lambda e: ci(e["reasoning_tokens_per_run"])),
        ("visible tokens per task", lambda e: ci(e["visible_tokens_per_run"])),
        (
            "cache hit rate after the first call",
            lambda e: f"{e['cache_hit_rate_after_first_call']:.0%}",
        ),
        ("time waiting for first tokens", lambda e: pct(e, "waiting_for_first_token_s")),
        ("time writing", lambda e: pct(e, "writing_s")),
        ("time in tools", lambda e: pct(e, "tools_s", ".1%")),
        ("cost per task (estimate)", lambda e: f"${e['cost_usd_per_run']:.4f}"),
    ]
    for label, fn in rows:
        cells = " | ".join(fn(e) for e in s["by_effort"].values())
        lines.append(f"| {label} | {cells} |")
    if "paired_task_time_s" in s:
        p = s["paired_task_time_s"]
        slower = [k for k in p if k.startswith("pairs_where")][0]
        lines += [
            "", f"Paired by run number, {p['difference']}: {ci(p, ' s')}. "
            f"{p[slower]} of {p['n']} pairs were slower at the higher effort.",
        ]
    f = s["first_token_vs_reasoning_tokens"]
    lines += [
        "", f"Wait for the first token against reasoning tokens, {f['n']} calls after "
        f"the first: {f['intercept_s']} s plus {f['s_per_token'] * 1000:.2f} ms per "
        f"reasoning token, which is about {f['implied_thinking_speed_tok_s']} reasoning "
        f"tokens per second ({f['implied_speed_lo']} to {f['implied_speed_hi']}). "
        f"Only {f['calls_with_reasoning']} of those calls had any reasoning tokens.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--check", action="store_true", help="fail if results/ is out of date"
    )
    args = ap.parse_args(argv)
    raw_files = sorted(RAW.glob("*.jsonl"))
    if not raw_files:
        print("No runs in data/raw/ yet. Run trace_agent.py first.", file=sys.stderr)
        return 2
    summary, traces, md = analyse(raw_files)
    outputs = {"summary.json": json.dumps(summary, indent=1) + "\n", "summary.md": md}
    for effort, t in traces.items():
        outputs[f"trace-{effort}.json"] = json.dumps(t, indent=1) + "\n"
    if args.check:
        stale = [
            name for name, text in outputs.items()
            if not (OUT / name).exists() or (OUT / name).read_text() != text
        ]
        if stale:
            print("results/ is out of date: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("results/ matches the raw runs.")
        return 0
    OUT.mkdir(exist_ok=True)
    for name, text in outputs.items():
        (OUT / name).write_text(text)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
