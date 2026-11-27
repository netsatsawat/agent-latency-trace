"""Checks for analyze.py. None of them call the API, so they run anywhere, CI included."""
import contextlib
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze  # noqa: E402


def make_call(step=2, reasoning=0, output=60, cached=1900, deltas=None):
    """A model call shaped like the ones trace_agent.py records."""
    deltas = deltas if deltas is not None else [[1.0, 0, 5], [1.2, 0, 5], [1.5, 0, 5]]
    return {
        "step": step, "sent_s": 3.0, "tool_calls": ["edit_file"],
        "usage": {
            "input_tokens": 2000, "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": output,
            "output_tokens_details": {"reasoning_tokens": reasoning},
        },
        "events": [
            [0.3, "response.created", ""],
            [0.9, "response.output_item.added", "reasoning"],
            [0.9, "response.output_item.done", "reasoning"],
            [1.0, "response.output_item.added", "function_call"],
            [1.5, "response.output_item.done", "function_call"],
            [1.51, "response.completed", ""],
        ],
        "deltas": deltas,
    }


def make_run(run=1, effort="medium", calls=None, success=True, task="checkout"):
    """A run shaped like the ones trace_agent.py writes, 2 calls by default."""
    calls = calls if calls is not None else [make_call(step=1), make_call(step=2)]
    return {
        "task": task, "model": "gpt-6.2-sol", "effort": effort, "run": run,
        "success": success, "total_s": 6.0, "cost_usd": 0.004,
        "prices_per_mtok": {"input": 2.5, "cached": 0.1, "output": 10.0},
        "calls": calls,
        "tools": [{"step": 1, "name": "edit_file", "start_s": 1.6, "end_s": 1.7}],
    }


class TempRuns(unittest.TestCase):
    """Writes small run files to a temporary folder, never to data/raw/."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, name, runs):
        path = self.dir / name
        path.write_text("".join(json.dumps(r) + "\n" for r in runs))
        return path


class TestCallMetrics(unittest.TestCase):
    def test_first_and_last_token_ignore_the_reasoning_item(self):
        m = analyze.call_metrics(make_call(reasoning=40))
        self.assertEqual(m["first_token_s"], 1.0)
        self.assertEqual(m["last_token_s"], 1.5)

    def test_visible_tokens_leave_out_reasoning(self):
        m = analyze.call_metrics(make_call(reasoning=40, output=100))
        self.assertEqual(m["visible_tokens"], 60)
        self.assertAlmostEqual(m["writing_speed"], 60 / 0.5)

    def test_short_calls_get_no_writing_speed(self):
        m = analyze.call_metrics(make_call(output=10))
        self.assertIsNone(m["writing_speed"])

    def test_a_tool_call_with_no_deltas_still_has_a_first_token(self):
        m = analyze.call_metrics(make_call(deltas=[]))
        self.assertEqual(m["first_token_s"], 1.0)


class TestStatistics(unittest.TestCase):
    def test_median_ci_is_repeatable_with_the_same_seed(self):
        values = [9.1, 9.3, 8.8, 10.2, 9.9, 9.0]
        a = analyze.median_ci(values, random.Random(1))
        b = analyze.median_ci(values, random.Random(1))
        self.assertEqual(a, b)
        self.assertLessEqual(a["lo"], a["median"])
        self.assertLessEqual(a["median"], a["hi"])

    def test_fit_line_recovers_a_known_line(self):
        fit = analyze.fit_line([0, 100, 200], [1.0, 2.5, 4.0])
        self.assertAlmostEqual(fit["intercept_s"], 1.0)
        self.assertAlmostEqual(fit["s_per_token"], 0.015)

    def test_fit_line_gives_none_when_every_x_is_the_same(self):
        self.assertIsNone(analyze.fit_line([0, 0, 0], [1.0, 1.1, 1.2]))


class TestNoReasoningTokens(TempRuns):
    def test_runs_with_no_reasoning_give_no_fit_and_no_crash(self):
        runs = [
            make_run(run=i, effort=e) for i in (1, 2) for e in ("low", "medium")
        ]
        summary, traces, md = analyze.analyse([self.write("runs.jsonl", runs)])
        self.assertIsNone(summary["first_token_vs_reasoning_tokens"])
        self.assertIn("there is no thinking fit", md)
        self.assertEqual(set(traces), {"low", "medium"})


class TestCallsThatCannotBeTimed(TempRuns):
    def test_a_call_with_an_api_error_is_left_out_and_its_run_fails(self):
        error_call = {"step": 3, "sent_s": 5.0, "error": "HTTP 500: server error"}
        calls = [make_call(step=1), make_call(step=2), error_call]
        runs = [make_run(run=1, calls=calls, success=False), make_run(run=2)]
        summary, _, md = analyze.analyse([self.write("runs.jsonl", runs)])
        e = summary["by_effort"]["medium"]
        self.assertEqual(e["runs"], 2)
        self.assertEqual(e["succeeded"], 1)
        self.assertEqual(e["first_token_s_per_call"]["n"], 4)
        self.assertEqual(summary["all_runs"]["calls_left_out"], 1)
        self.assertEqual(summary["all_runs"]["model_calls_per_run"]["max"], 3)
        self.assertIn("API error", md)

    def test_a_failed_call_with_no_visible_output_is_left_out(self):
        failed = make_call(step=2, deltas=[])
        failed["usage"] = {}
        failed["events"] = [[0.3, "response.created", ""], [2.0, "response.failed", ""]]
        self.assertIsNone(analyze.call_metrics(failed))
        # trace_agent.py can record such a run as passed. analyze.py counts it as failed.
        run = make_run(calls=[make_call(step=1), failed], success=True)
        self.assertFalse(analyze.run_metrics(dict(run, source="x"))["success"])


class TestMixedRunsAreRefused(TempRuns):
    def test_two_tasks_are_refused(self):
        a = self.write("a.jsonl", [make_run(task="checkout")])
        b = self.write("b.jsonl", [make_run(task="billing")])
        with self.assertRaises(analyze.RunFileError):
            analyze.analyse([a, b])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(analyze.main([str(a), str(b)]), 2)
        self.assertIn("more than one task", err.getvalue())

    def test_two_models_are_refused(self):
        other = dict(make_run(run=2), model="gpt-6.1")
        path = self.write("runs.jsonl", [make_run(run=1), other])
        with self.assertRaisesRegex(analyze.RunFileError, "more than one model"):
            analyze.analyse([path])

    def test_runs_pair_by_file_and_run_number(self):
        a = self.write("a.jsonl", [make_run(1, "medium"), make_run(1, "xhigh")])
        b = self.write("b.jsonl", [make_run(1, "medium"), make_run(1, "xhigh")])
        summary, _, _ = analyze.analyse([a, b])
        self.assertEqual(summary["paired_task_time_s"]["n"], 2)


class TestCommittedResults(unittest.TestCase):
    def test_results_match_the_raw_runs(self):
        if not list(analyze.RAW.glob("*.jsonl")):
            self.skipTest("no raw runs committed")
        self.assertEqual(analyze.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
