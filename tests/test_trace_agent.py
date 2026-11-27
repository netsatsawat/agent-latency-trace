"""Checks for trace_agent.py. None of them call the API. They run anywhere, CI included.

A fake connection plays back a stream shaped like the ones the Responses API sends.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trace_agent  # noqa: E402

WORDS = "the model's own words"
USAGE = {
    "input_tokens": 1487, "input_tokens_details": {"cached_tokens": 1484},
    "output_tokens": 51, "output_tokens_details": {"reasoning_tokens": 0},
}


def sse(*messages) -> bytes:
    """Server-sent events as the API streams them: an event line, a data line, a gap."""
    return b"".join(
        f"event: {m['type']}\ndata: {json.dumps(m)}\n\n".encode() for m in messages
    )


def message_stream() -> bytes:
    """One reply with a short text answer, from the first event to the last."""
    output = [{"type": "message", "content": [{"type": "output_text", "text": WORDS}]}]
    item = {"output_index": 0, "item": {"type": "message"}}
    return sse(
        {"type": "response.created", "response": {}},
        dict(item, type="response.output_item.added"),
        {"type": "response.output_text.delta", "output_index": 0, "delta": WORDS},
        dict(item, type="response.output_item.done"),
        {
            "type": "response.completed",
            "response": {"status": "completed", "usage": USAGE, "output": output},
        },
    )


class FakeResponse:
    def __init__(self, body: bytes, status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self):
        return self.body.read()

    def readline(self):
        return self.body.readline()

    def getheader(self, name):
        return self.headers.get(name)


class FakeConnection:
    """Takes the place of http.client.HTTPSConnection and plays back one reply."""

    def __init__(self, response: FakeResponse):
        self.response = response

    def request(self, method, path, body=None, headers=None):
        self.payload = json.loads(body)

    def getresponse(self):
        return self.response


def model_reply(*output) -> dict:
    """What stream_call returns for one call, with the output items given."""
    return {
        "events": [], "deltas": [], "status": "completed", "usage": USAGE,
        "output": list(output), "request_id": None, "processing_ms": None,
    }


def edit_call(call_id, old, new) -> dict:
    return {
        "type": "function_call", "call_id": call_id, "name": "edit_file",
        "arguments": json.dumps({"path": "prices.py", "old_text": old, "new_text": new}),
    }


class TempProject(unittest.TestCase):
    """A temporary project folder with one small file in it."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        (self.dir / "prices.py").write_text("x = 1\ny = 1\n")

    def tearDown(self):
        self._dir.cleanup()


class TestStreamCall(unittest.TestCase):
    def test_it_keeps_times_and_sizes_but_no_text(self):
        headers = {"x-request-id": "req_123", "openai-processing-ms": "812"}
        conn = FakeConnection(FakeResponse(message_stream(), headers=headers))
        call = trace_agent.stream_call(conn, {"model": "gpt-6.2-sol"}, "test-key")
        self.assertEqual(
            [e[1] for e in call["events"]],
            [
                "response.created", "response.output_item.added",
                "response.output_item.done", "response.completed",
            ],
        )
        self.assertEqual([d[1:] for d in call["deltas"]], [[0, len(WORDS)]])
        self.assertEqual(call["usage"], USAGE)
        self.assertEqual(call["request_id"], "req_123")
        self.assertEqual(call["processing_ms"], "812")
        # run_agent drops the output before it saves the call. The rest holds no text.
        call.pop("output")
        self.assertNotIn(WORDS, json.dumps(call))

    def test_an_error_event_raises(self):
        body = sse(
            {"type": "response.created", "response": {}},
            {"type": "error", "message": "The server is busy."},
        )
        with self.assertRaisesRegex(RuntimeError, "stream error"):
            trace_agent.stream_call(FakeConnection(FakeResponse(body)), {}, "test-key")

    def test_a_stream_with_no_final_event_raises(self):
        body = sse({"type": "response.created", "response": {}})
        with self.assertRaisesRegex(RuntimeError, "without a final response"):
            trace_agent.stream_call(FakeConnection(FakeResponse(body)), {}, "test-key")

    def test_an_http_error_raises_with_its_status(self):
        reply = FakeResponse(b'{"error": "Incorrect API key"}', status=401)
        with self.assertRaises(trace_agent.APIError) as caught:
            trace_agent.stream_call(FakeConnection(reply), {}, "test-key")
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("HTTP 401", str(caught.exception))


class TestTools(TempProject):
    def test_paths_outside_the_project_folder_are_refused(self):
        for name in ("../prices.py", "sub/prices.py"):
            with self.assertRaises(ValueError):
                trace_agent._inside(self.dir, name)
            args = json.dumps({"path": name})
            result = trace_agent.run_tool(self.dir, "read_file", args)
            self.assertTrue(result.startswith("Tool error"), result)

    def test_an_edit_needs_old_text_that_appears_exactly_once(self):
        for old in ("z = 3", "= 1"):
            args = json.dumps({"path": "prices.py", "old_text": old, "new_text": "= 2"})
            result = trace_agent.run_tool(self.dir, "edit_file", args)
            self.assertTrue(result.startswith("Edit failed"), result)
        self.assertEqual((self.dir / "prices.py").read_text(), "x = 1\ny = 1\n")

    def test_an_unknown_tool_and_bad_arguments_come_back_as_text(self):
        self.assertEqual(
            trace_agent.run_tool(self.dir, "delete_file", "{}"),
            "There is no tool called delete_file.",
        )
        for arguments in ('{"path": ', "[]"):
            result = trace_agent.run_tool(self.dir, "read_file", arguments)
            self.assertTrue(result.startswith("Tool error"), result)

    def test_the_tests_get_only_4_variables(self):
        done = subprocess.CompletedProcess([], 0, "OK", "")
        run = mock.Mock(return_value=done)
        secrets = {"OPENAI_API_KEY": "test-key", "GITHUB_TOKEN": "test-token"}
        with mock.patch.dict(os.environ, secrets):
            with mock.patch.object(trace_agent.subprocess, "run", run):
                passed, _ = trace_agent.run_tests(self.dir, sandbox=False)
        self.assertTrue(passed)
        env = run.call_args.kwargs["env"]
        self.assertLessEqual(set(env), {"PATH", "HOME", "LANG", "TMPDIR"})


class TestRunAgent(unittest.TestCase):
    def test_two_edits_to_one_file_in_one_step_both_land(self):
        first = model_reply(
            edit_call(
                "call_1", "return round(price - price * percent, 2)",
                "return round(price - price * percent / 100, 2)",
            ),
            edit_call(
                "call_2", "return round(amount * rate / 100, 2)",
                "return round(amount + amount * rate / 100, 2)",
            ),
        )
        answer = model_reply({"type": "message", "content": []})
        seen = {}

        def fake_tests(workdir, sandbox=True):
            seen["prices.py"] = (workdir / "prices.py").read_text()
            return True, "OK"

        stream_call = mock.Mock(side_effect=[first, answer])
        with mock.patch.multiple(
            trace_agent, stream_call=stream_call, run_tests=fake_tests
        ):
            run = trace_agent.run_agent(
                "checkout", "gpt-6.2-sol", "medium", "test-key",
                trace_agent.DEFAULT_PRICES, 5.0, sandbox=False,
            )
        self.assertEqual(run["stop"], "answered")
        self.assertIn("percent / 100", seen["prices.py"])
        self.assertIn("amount + amount", seen["prices.py"])
        a, b = run["tools"]
        self.assertLessEqual(a["end_s"], b["start_s"])


class TestMain(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.out = Path(self._dir.name) / "runs.jsonl"
        self.err = io.StringIO()

    def tearDown(self):
        self._dir.cleanup()

    def main(self, *args) -> int:
        with contextlib.ExitStack() as stack:
            key = {"OPENAI_API_KEY": "test-key"}
            stack.enter_context(mock.patch.dict(os.environ, key))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(self.err))
            return trace_agent.main(list(args))

    def test_another_model_needs_all_3_prices(self):
        self.assertEqual(self.main("--model", "gpt-x", "--price-input", "1"), 2)
        self.assertIn("all 3", self.err.getvalue())

    def test_it_stops_without_sandbox_exec_unless_told(self):
        run_agent = mock.Mock()
        no_which = mock.Mock(return_value=None)
        with mock.patch.object(trace_agent.shutil, "which", no_which):
            with mock.patch.object(trace_agent, "run_agent", run_agent):
                self.assertEqual(self.main("--runs", "1", "--out", str(self.out)), 2)
        run_agent.assert_not_called()
        self.assertIn("--no-sandbox", self.err.getvalue())

    def test_runs_must_be_1_or_more(self):
        with self.assertRaises(SystemExit):
            self.main("--runs", "0")

    def test_max_cost_must_be_above_0(self):
        with self.assertRaises(SystemExit):
            self.main("--max-cost", "0")

    def test_a_401_stops_the_batch(self):
        bad_key = mock.Mock(side_effect=trace_agent.APIError(401, "Incorrect API key"))
        no_tests = mock.Mock(return_value=(False, ""))
        with mock.patch.multiple(trace_agent, stream_call=bad_key, run_tests=no_tests):
            code = self.main("--runs", "3", "--no-sandbox", "--out", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", self.err.getvalue())
        lines = self.out.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["calls"][0]["http_status"], 401)

    def test_a_429_with_no_credit_stops_the_batch(self):
        no_credit = mock.Mock(side_effect=trace_agent.APIError(
            429, '{"error": {"code": "credit_balance_exhausted"}}'
        ))
        no_tests = mock.Mock(return_value=(False, ""))
        with mock.patch.multiple(trace_agent, stream_call=no_credit, run_tests=no_tests):
            code = self.main("--runs", "3", "--no-sandbox", "--out", str(self.out))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.out.read_text().splitlines()), 1)

    def test_a_rate_limit_429_does_not_stop_the_batch(self):
        busy = mock.Mock(side_effect=trace_agent.APIError(
            429, '{"error": {"code": "rate_limit_exceeded"}}'
        ))
        no_tests = mock.Mock(return_value=(False, ""))
        with mock.patch.multiple(trace_agent, stream_call=busy, run_tests=no_tests):
            code = self.main("--runs", "3", "--no-sandbox", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.out.read_text().splitlines()), 6)


if __name__ == "__main__":
    unittest.main()
