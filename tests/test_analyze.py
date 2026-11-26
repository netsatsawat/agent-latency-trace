"""Checks for analyze.py. None of them call the API, so they run anywhere, CI included."""
import random
import sys
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


class TestCommittedResults(unittest.TestCase):
    def test_results_match_the_raw_runs(self):
        if not list(analyze.RAW.glob("*.jsonl")):
            self.skipTest("no raw runs committed")
        self.assertEqual(analyze.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
