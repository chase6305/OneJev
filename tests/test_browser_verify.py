"""Deterministic regression tests; no GPU or running browser is required."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.browser_verify import Prediction, Recorder, STATUSES, decide, main, record_invalid_response, threshold_value
from qev import Client, Noul


def answers(status="unpaid", completed=0.01, confidence=0.9):
    distribution = {key: (confidence if key == status else (1 - confidence) / 4) for key in STATUSES}
    return {"completed": {"type": "noul", "noul": completed},
            "status": {"type": "choice", "choice": status, "probabilities": distribution}}


class DecisionTests(unittest.TestCase):
    def decision(self, status="unpaid", completed=0.01, confidence=0.9, page_status=None, attempts=0):
        prediction = Prediction(completed, status, confidence)
        return decide(prediction, page_status or status, attempts, max_attempts=2, threshold=0.85,
                      min_confidence=0.6).action

    def test_success_requires_model_and_page_confirmation(self):
        self.assertEqual(self.decision("paid", completed=0.98), "verified")

    def test_model_completion_cannot_override_unpaid_page(self):
        self.assertEqual(self.decision("paid", completed=0.99, page_status="unpaid"), "review")
        self.assertEqual(self.decision("unpaid", completed=0.99), "review")

    def test_paid_page_is_never_submitted_again(self):
        for status in STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self.decision(status, completed=0.1, page_status="paid"), "review")

    def test_disagreement_and_low_confidence_stop_actions(self):
        self.assertEqual(self.decision("retry", page_status="unpaid"), "review")
        self.assertEqual(self.decision("unpaid", confidence=0.59), "review")
        self.assertEqual(self.decision("paid", completed=0.99, confidence=0.59), "review")

    def test_first_attempt_counts_toward_limit(self):
        self.assertEqual(self.decision("unpaid", attempts=0), "pay")
        self.assertEqual(self.decision("retry", attempts=1), "retry")
        self.assertEqual(self.decision("retry", attempts=2), "exhausted")
        self.assertEqual(self.decision("unpaid", attempts=2), "exhausted")

    def test_in_flight_payment_is_waited_for(self):
        self.assertEqual(self.decision("processing", attempts=1), "wait")
        self.assertEqual(self.decision("processing", attempts=2), "wait")

    def test_declined_payment_requires_review(self):
        self.assertEqual(self.decision("blocked", attempts=1), "review")


class PredictionTests(unittest.TestCase):
    def test_reads_probability_from_selected_option(self):
        self.assertEqual(Prediction.from_answers(answers("paid", 0.98)), Prediction(0.98, "paid", 0.9))

    def test_rejects_invalid_completion_values(self):
        for value in (float("nan"), float("inf"), -0.1, 1.1, True, "0.9", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Prediction.from_answers(answers(completed=value))

    def test_rejects_unknown_or_missing_options(self):
        data = answers()
        data["status"]["choice"] = "click_arbitrary_button"
        with self.assertRaises(ValueError):
            Prediction.from_answers(data)
        data = answers()
        del data["status"]["probabilities"]["blocked"]
        with self.assertRaises(ValueError):
            Prediction.from_answers(data)

    def test_rejects_malformed_distributions(self):
        for value in (float("nan"), -0.1, 1.1, True):
            data = answers()
            data["status"]["probabilities"]["paid"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                Prediction.from_answers(data)
        data = answers()
        data["status"]["probabilities"] = dict.fromkeys(STATUSES, 0.01)
        with self.assertRaises(ValueError):
            Prediction.from_answers(data)

    def test_rejects_choice_that_does_not_match_distribution(self):
        data = answers("unpaid")
        data["status"]["choice"] = "paid"
        with self.assertRaises(ValueError):
            Prediction.from_answers(data)


class ClientMetadataTests(unittest.TestCase):
    def test_preserves_qev_and_flide_onejev_metadata(self):
        for key in ("qev", "onejev"):
            payload = {"model": "OneJev1.6-0.8B", "answers": {"done": {"type": "noul", "noul": 0.95}},
                       "usage": {"input_tokens": 111, "output_tokens": 1},
                       key: {"timing": {"total_s": 0.067}, "prompt_version": "onejev-labels-v2"}}
            with self.subTest(key=key), patch.object(Client, "_post", return_value=(
                    payload, {"x-typesafe-request-id": "test-request"})) as post:
                response = Client().system_one("state", {"done": Noul("Task is complete.")})
                self.assertEqual(response.meta, payload[key])
                self.assertEqual(response.request_id, "test-request")
                self.assertEqual(response.answers["done"].noul, 0.95)
                self.assertEqual(post.call_args.args[1]["questions"]["done"]["type"], "noul")

    def test_metadata_is_optional(self):
        with patch.object(Client, "_post", return_value=({"model": "test", "answers": {}}, {})):
            self.assertEqual(Client().system_one("state", {}).meta, {})


class ArtifactTests(unittest.TestCase):
    def test_invalid_response_still_leaves_inspectable_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = Recorder(Path(temp))
            record_invalid_response(recorder, {"answers": {"completed": float("nan")}, "screenshot": "00.png"},
                                    ValueError("Invalid completion probability."))
            with patch("builtins.print"):
                recorder.finish("error", "Invalid completion probability.")
            report = json.loads((recorder.path / "report.json").read_text())
            observation = report["observations"][0]
            self.assertEqual(observation["decision"]["action"], "error")
            self.assertIn("nan", observation["invalid_answers"])
            self.assertEqual(observation["screenshot"], "00.png")

    def test_preserves_observations_and_uses_distinct_run_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Recorder(Path(temp)), Recorder(Path(temp))
            self.assertNotEqual(first.path, second.path)
            first.append({"step": 0, "screenshot": "00.png", "label": "待判断"})
            with patch("builtins.print"):
                first.finish("review", "Below threshold.")
            report = json.loads((first.path / "report.json").read_text())
            records = [json.loads(row) for row in (first.path / "observations.jsonl").read_text().splitlines()]
            self.assertEqual(report["observations"], records)
            self.assertEqual(report["result"], "review")


class ArgumentTests(unittest.TestCase):
    def test_rejects_thresholds_that_accept_ambiguous_predictions(self):
        for value in ("nan", "inf", "-1", "0", "0.5", "1.01"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                threshold_value(value)
        self.assertEqual(threshold_value("0.85"), 0.85)

    def test_external_page_requires_http_and_completion_statement(self):
        for args in (["--url", "file:///etc/passwd", "--success", "done"],
                     ["--url", "https://example.com"], ["--success", "done"],
                     ["--url", "http:", "--success", "done"],
                     ["--url", "http://[broken", "--success", "done"],
                     ["--url", "https://example.com", "--success", "   "]):
            with self.subTest(args=args), patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                main(args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
