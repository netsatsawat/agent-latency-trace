# agent-latency-trace

**Your agent feels slow, and the usual advice (a smaller model, a shorter prompt) does not say
where the seconds go. This repo times a real agent one model call at a time, so you can see
it, and every number here can be checked from the raw logs without an API key.**

It runs a small coding agent on OpenAI's Responses API. The agent fixes a broken checkout
function with 3 tools: read a file, edit a file, run the tests. For each model call it records
when every streamed event arrived, how many tokens the model read, thought and wrote, how much
of the prompt came from the cache, and how long each tool took. The data behind every table
below is in `data/raw/`, and `make check` rebuilds the tables from it.

Companion to the article *Why Is Your AI Agent So Slow?*, to be published soon on
[Medium](https://medium.com/@net_satsawat).

![One agent task drawn as a trace: You, Model and Tool lanes on a time axis](docs/trace.gif)

*A run from the middle of the 10 at xhigh effort, as a trace. The width of each block is its time.*

## What it found

GPT-6.2 Sol, 10 runs at medium effort and 10 at xhigh, run one after the other on
26 November 2026 from one laptop over the public API. Every run passed the tests. Medians, with
95 percent bootstrap intervals in brackets:

| | medium | xhigh |
|---|---|---|
| task time | 9.3 s (9.0 to 9.9) | 11.2 s (10.9 to 11.8) |
| model calls per task | 5 | 4 |
| wait for the first token, per call | 1.08 s | 1.34 s |
| writing speed | about 95 tokens a second | about 100 tokens a second |
| reasoning tokens per task | 0 | 137 |
| share of the task spent waiting for first tokens | 61% | 60% |
| share spent writing | 37% | 38% |
| share spent in tools | 0.6% | 0.5% |
| cost per task, estimated from list prices | $0.0049 | $0.0087 |

The full table, generated from the logs, is [results/summary.md](results/summary.md).

**1. Most of the time went to waiting for each call's first token.** Every call waited about
1.1 s before anything came back, and a task made 4 to 6 calls. Each call then wrote only 14 to
398 visible tokens, so the fixed wait outweighed the writing: about 60 percent of the task
against just under 40 for writing. For a task made of short steps, the number of calls is the lever. Ask the model for
independent tools in one step, which this agent already allows.

**2. More effort made the task slower, even with fewer calls.** At xhigh the model planned one
bigger edit and finished in 4 calls instead of 5, but it was slower in all 10 paired runs, by
2.0 s at the median (0.8 to 2.6). At medium it did not think at all on this task. Raise the
effort only where a measured gain in passed tasks pays for the seconds.

**3. Thinking is hidden inside the wait, and it was slower per token than writing.** The API
sends no thinking text and no start time for it. Fitting the wait for the first token against
the reasoning tokens in the usage data gives about 15 ms per reasoning token, about 66 tokens a
second (60 to 72), against about 95 to 100 for visible writing. Only 10 calls had any reasoning, so
treat that number as rough.

**4. Where the system prompt goes decides whether it is cached.** The first pilot sent it in the
top-level `instructions` field and got 0 cached tokens on every call. OpenAI's prompt caching
guide says GPT-6.2 models place `instructions` after the input, past the part the cache covers.
Sent as the first `developer` message instead, 92 to 99 percent of the input after the first
call came from the cache.

**5. The tools took almost no time.** Reading a file, editing it and running the tests took
milliseconds. Real tools that call a network or a database will take a bigger share.

## What a call's numbers mean

Each call is sent, waits, then streams. The script keeps the time of every event and the
`usage` block the API returns. [analyze.py](analyze.py) turns those into:

- **wait for the first token:** from sending the request to the first visible output, text or the
  start of a tool call. It covers the network, the provider's queue, reading the part of the
  prompt that is not cached, and any hidden thinking.
- **reasoning tokens:** `usage.output_tokens_details.reasoning_tokens`. The model thinks before
  its first visible token, so this time sits inside the wait.
- **writing speed:** visible tokens (output tokens minus reasoning tokens) divided by the time
  from the first visible token to the last. Calls under 20 visible tokens or 0.25 s are too
  short to time and are left out.
- **cache hit rate:** `usage.input_tokens_details.cached_tokens` divided by all input tokens.
- **tool time:** from the moment a tool starts to the moment its result is ready.

## Check the numbers without an API key

```bash
git clone https://github.com/netsatsawat/agent-latency-trace
cd agent-latency-trace
make check
```

`make check` runs the tests and recomputes every number in `results/` from the raw logs. It
fails if any of them would change. It needs Python 3.9 or later and nothing else, and it is
what the CI job runs on every push.

## Run it yourself

```bash
export OPENAI_API_KEY=...
python3 trace_agent.py --task checkout --model gpt-6.2-sol --efforts medium xhigh --runs 10 --max-cost 5
python3 analyze.py
```

The 20 runs above cost about $0.14 by list prices and took about 4 minutes. The script stops
before the `--max-cost` cap, which it counts with the uncached input priced at the cache-write
rate, so its total errs on the high side. Your numbers will differ with the model, the effort,
the time of day and where you run from, which is the point: time your own agent before you
change it. A second task, `--task billing`, has date bugs that need more reasoning.

On macOS the tests run under `sandbox-exec`, with the network blocked and writes allowed only
inside the run's own temporary folder, because the code under test is code the model wrote.
On other systems they run without that sandbox.

## Files

| path | what it is |
|---|---|
| [trace_agent.py](trace_agent.py) | runs the agent and records every call, standard library only |
| [analyze.py](analyze.py) | turns the raw runs into `results/`, with `--check` for CI |
| `tasks/checkout/`, `tasks/billing/` | the small projects the agent fixes, exactly as they were run |
| `prompts/` | the system prompt and the task prompts, exactly as they were sent |
| `data/raw/` | one JSON line per run: every event time, the usage data, the tool times |
| `data/pilot/` | the pilot runs that shaped the design, kept for the record |
| `results/` | the summary and the median trace for each effort level, generated |
| `tests/` | offline tests, including one that checks `results/` against `data/raw/` |

The logs keep the time and size of every streamed piece but none of the text the model wrote.

## How the design got here

The pilot runs in `data/pilot/` changed the experiment in 3 ways, and they are kept so you can
see why.

1. The first pilot, on an earlier one-bug version of the checkout task, put the system prompt in
   `instructions` and got no cache hits at all. It now goes first in the input as a developer
   message, with a fixed `prompt_cache_key`.
2. The first version opened a new connection for every call. It now keeps one connection per
   run, as an SDK client would.
3. On the checkout task the model wrote no reasoning tokens at low, medium or high effort, and
   none on the billing task at medium or high. At xhigh it thought on the step where it wrote
   the fix. So the final runs compare medium, the usual setting, with xhigh.

## Limits

- One model, one small task and one network location. A bigger task with longer outputs will
  spend a larger share of its time writing, which is what the article's sources found for
  coding agents on self-hosted models.
- 10 runs per effort level. The intervals above say how much that matters for each number.
- The tools are local and fast. Tools that call a network will change the shares.
- The thinking time in the GIF is an estimate: the extra wait for the first token over a call
  that did not think. The API does not report it.
- Costs are estimates from list prices on 26 November 2026, not from the bill.

## License

MIT. See [LICENSE](LICENSE).
