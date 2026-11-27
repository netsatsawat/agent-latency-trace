# Data

Each `.jsonl` file here holds runs of `trace_agent.py`. JSONL means one JSON object per
line, and each line is one run. All times are in seconds.

## The fields of a run

| field | what it is |
|---|---|
| `task`, `model`, `effort`, `run` | what ran. The 2 effort levels take turns and share a run number. A pair is the 2 runs with one run number in one file |
| `started_utc` | when the run started, in UTC |
| `total_s` | how long the run took, tools included. We stop the clock before the last test run, which decides `success` |
| `stop` | why the agent stopped: `answered`, `max_calls`, `spending_cap` or `api_error` |
| `success` | whether the tests passed after the agent answered. A run that stopped for any other reason counts as failed |
| `model_calls` | how many model calls the run made |
| `cost_usd` | the estimated cost of the run, in US dollars |
| `prices_per_mtok` | the prices we used for that estimate, in US dollars per million tokens |
| `python`, `os` | the Python version and the operating system that ran the script |
| `calls` | one entry per model call, with the fields below |
| `tools` | one entry per tool call, with its `step`, the tool's `name`, and `start_s` and `end_s` |

## The fields of a model call

| field | what it is |
|---|---|
| `step` | the call's number in the run, from 1 |
| `sent_s` | when we sent the call |
| `events` | one `[time, event type, item type]` for each event the API streamed |
| `deltas` | one `[time, output index, characters]` for each piece of text or tool arguments. We keep the size of each piece and none of its text |
| `status` | the status of the response at the end of the stream, such as `completed` |
| `usage` | the token counts, as the API returned them |
| `output_types` | the kind of each output item, such as `function_call` or `message` |
| `tool_calls` | the names of the tools the model asked for |
| `error` | only on a call that broke off before the reply finished: an HTTP error, a lost connection or an error event. Such a call has no `usage`. A reply that ended as failed or incomplete has no `error`, and its `status` shows how it ended |

The times in `events` and `deltas` count from when we sent that call. All the other times
count from the start of the run.

## Which runs are where

`data/raw/` holds the 20 runs the README quotes. The file name has the date only, because
the version of the script that wrote it named files that way.

`data/pilot/` holds the pilot runs. We wrote them with an earlier version of the script.
Pilot files 1 to 4 have no `task` field. Pilot 1 used an earlier checkout task with one bug,
which is not in this repo.

The current script writes new runs to `out/runs/`, with the date and the time in each file
name. It also saves fields that our files do not have:

- `script_commit` on each run: the git commit the script ran from.
- `request_id` and `processing_ms` on each call: the `x-request-id` and
  `openai-processing-ms` response headers, as text.
- `http_status` on a call that failed: the HTTP status code, when the API sent one.

## The trace files in results/

`results/trace-medium.json` and `results/trace-xhigh.json` each hold one middle run by task
time. In them, `sent_s` and `tool_spans` count from the start of the run. `first_token_s`
and `done_s` count from when we sent that call. `reading_and_queue_s`, `thinking_s` and
`writing_s` are lengths of time. `thinking_s` is an estimate. See Limits in the main
README.

We drew `docs/trace.gif` from `results/trace-xhigh.json` with a script for Remotion, a tool
that makes videos from code. That script is not in this repo.
