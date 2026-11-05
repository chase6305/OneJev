"""Shared screenshot decision policy for the CLI demo and interactive Studio."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

STATUSES = {
    "unpaid": "The invoice is unpaid and ready for a payment attempt.",
    "retry": "Payment failed temporarily; the invoice is unpaid and payment can be retried.",
    "processing": "A payment is processing and its result is not yet confirmed.",
    "paid": "Payment succeeded and the invoice is paid in full.",
    "blocked": "Payment was declined and manual review is required.",
}
GOAL = "Pay ACME invoice INV-1042."
SUCCESS = "Invoice INV-1042 is marked Paid and the payment succeeded."


def probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a probability, got {value!r}")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class Prediction:
    completed: float
    status: str
    confidence: float

    @classmethod
    def from_answers(cls, answers: dict[str, dict]) -> Prediction:
        completed = probability(answers["completed"]["noul"], "completed.noul")
        status = answers["status"]["choice"]
        distribution = answers["status"]["probabilities"]
        if status not in STATUSES or set(distribution) != set(STATUSES):
            raise ValueError("status answer must use the requested payment states")
        probabilities = {k: probability(v, f"status.{k}") for k, v in distribution.items()}
        if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01):
            raise ValueError("status probabilities must sum to 1")
        confidence = probabilities[status]
        if confidence < max(probabilities.values()):
            raise ValueError("status.choice must select a most probable option")
        return cls(completed, status, confidence)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def decide(prediction: Prediction, page_status: str, attempts: int, max_attempts: int,
           threshold: float, min_confidence: float) -> Decision:
    """Keep demo actions bounded and require independent page evidence for success."""
    confident = prediction.confidence >= min_confidence
    if page_status == "paid":
        if prediction.status == "paid" and prediction.completed >= threshold and confident:
            return Decision("verified", "Model judgment and the page's Paid state agree.")
        return Decision("review", "The page is paid, but the model did not confirm it confidently.")
    if prediction.completed >= threshold or prediction.status == "paid":
        return Decision("review", "The model reports completion but the page is not paid.")
    if prediction.status != page_status:
        return Decision("review", "Model judgment and the page state disagree.")
    if not confident:
        return Decision("review", "The model's state confidence is below the configured threshold.")
    if prediction.status == "blocked":
        return Decision("review", "Payment was declined; manual review is required.")
    if prediction.status == "processing":
        return Decision("wait", "Wait for the existing payment attempt to finish.")
    if attempts >= max_attempts:
        return Decision("exhausted", "The payment attempt limit has been reached.")
    return Decision("retry" if prediction.status == "retry" else "pay", "An allowed payment attempt is available.")

