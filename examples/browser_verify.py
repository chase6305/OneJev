"""Playwright + OneJev: observe a page, verify its state, and retry a local task.

    pip install -e '.[browser]'
    python -m playwright install chromium
    python examples/browser_verify.py

See browser_verify.md for the Chinese and English walkthrough and exit codes.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from uuid import uuid4

from qev import Choice, Client, Noul, Score

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


class Recorder:
    def __init__(self, root: Path):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.path = root.expanduser().resolve() / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.observations: list[dict] = []

    def append(self, observation: dict) -> None:
        encoded = json.dumps(observation, ensure_ascii=False, allow_nan=False)
        with (self.path / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        self.observations.append(observation)

    def finish(self, result: str, reason: str, **details: Any) -> None:
        report = {"result": result, "reason": reason, **details, "observations": self.observations}
        (self.path / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"Result: {result} — {reason}")
        print(f"Artifacts: {self.path}")


def observe(page, client: Client, recorder: Recorder, *, goal: str, success: str,
            demo: bool, attempts: int) -> dict:
    step = len(recorder.observations)
    screenshot = f"{step:02d}.png"
    png = page.screenshot(path=str(recorder.path / screenshot), animations="disabled", caret="hide")
    captured_status = page.locator("body").get_attribute("data-state") if demo else None
    questions: dict = {"completed": Noul(success)}
    if demo:
        questions["status"] = Choice("What is the current payment state shown on the screen?", STATUSES)
        questions["progress"] = Score("How far has payment progressed?", [
            "No payment attempt is shown.", "Payment was attempted but is incomplete.", "Payment is complete."])
    started = time.perf_counter()
    response = client.system_one(
        state={"task": goal, "screen": "<image:1>"}, questions=questions,
        media=[{"type": "image", "data": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}])
    observation = {
        "step": step, "attempts": attempts, "screenshot": screenshot,
        "model": response.model, "answers": {k: v.raw for k, v in response.answers.items()},
        "usage": response.usage, "request_id": response.request_id, "meta": response.meta,
        "request_s": time.perf_counter() - started,
    }
    if demo:
        observation["captured_status"] = captured_status
        observation["page_status"] = page.locator("body").get_attribute("data-state")
    return observation


def record_invalid_response(recorder: Recorder, observation: dict, error: Exception) -> None:
    observation["decision"] = {"action": "error", "reason": str(error)}
    try:
        json.dumps(observation, allow_nan=False)
    except ValueError:
        # Preserve non-finite server values as text, keeping the report valid JSON.
        observation["invalid_answers"] = repr(observation.pop("answers"))
    recorder.append(observation)


def run_demo(page, client: Client, recorder: Recorder, args) -> Decision:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    fixture = Path(__file__).with_name("browser_task.html").resolve().as_uri()
    page.goto(f"{fixture}?scenario={args.scenario}", wait_until="load")
    attempts = 0
    for index in range(args.max_observations):
        observation = observe(page, client, recorder, goal=GOAL, success=SUCCESS, demo=True, attempts=attempts)
        try:
            prediction = Prediction.from_answers(observation["answers"])
        except (ValueError, KeyError, TypeError) as exc:
            record_invalid_response(recorder, observation, exc)
            raise
        if observation["captured_status"] != observation["page_status"]:
            decision = Decision("wait", "The page changed during inference; take a fresh screenshot.")
        else:
            decision = decide(prediction, observation["page_status"], attempts, args.max_attempts,
                              args.threshold, args.min_confidence)
        if decision.action in ("pay", "retry", "wait") and index + 1 == args.max_observations:
            decision = Decision("exhausted", "The observation limit has been reached.")
        observation["decision"] = asdict(decision)
        recorder.append(observation)
        print(f"[{observation['step']}] page={observation['page_status']} model={prediction.status} "
              f"p(done)={prediction.completed:.3f} confidence={prediction.confidence:.3f} → {decision.action}")
        if decision.action not in ("pay", "retry", "wait"):
            return decision
        if decision.action in ("pay", "retry"):
            page.locator("#pay").click()
            attempts += 1
        try:
            page.wait_for_function("document.body.dataset.state !== 'processing'", timeout=args.page_timeout * 1000)
        except PlaywrightTimeoutError:
            # Observe again rather than clicking a payment button twice while it is processing.
            pass
    return Decision("exhausted", "The observation limit has been reached.")


def run_check(page, client: Client, recorder: Recorder, args) -> Decision:
    response = page.goto(args.url, wait_until="load")
    if response is not None and response.status >= 400:
        raise ValueError(f"Page returned HTTP {response.status}: {args.url}")
    if args.wait_for:
        page.locator(args.wait_for).wait_for(state="visible")
    observation = observe(page, client, recorder, goal=args.goal or args.success, success=args.success,
                          demo=False, attempts=0)
    try:
        completed = probability(observation["answers"]["completed"]["noul"], "completed.noul")
    except (ValueError, KeyError, TypeError) as exc:
        record_invalid_response(recorder, observation, exc)
        raise
    decision = Decision("verified" if completed >= args.threshold else "review",
                        f"Screenshot completion probability {completed:.3f}; threshold {args.threshold:.3f}.")
    observation["decision"] = asdict(decision)
    recorder.append(observation)
    return decision


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return parsed


def threshold_value(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.5 < parsed <= 1:
        raise argparse.ArgumentTypeError("must be greater than 0.5 and at most 1")
    return parsed


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="System One endpoint; defaults to QEV_BASE_URL or localhost:8000")
    ap.add_argument("--model", default="jev-latest", help="Model name or server alias")
    ap.add_argument("--channel", help="Installed browser channel, e.g. chrome; default: Playwright Chromium")
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    ap.add_argument("--output", type=Path, default=Path("runs/browser"), help="Parent directory for per-run artifacts")
    ap.add_argument("--threshold", type=threshold_value, default=0.85, help="Completion threshold (default: 0.85)")
    ap.add_argument("--min-confidence", type=threshold_value, default=0.6,
                    help="Minimum probability of the selected demo state (default: 0.6)")
    ap.add_argument("--max-attempts", type=positive_int, default=2, help="Total demo payment attempts, including the first")
    ap.add_argument("--max-observations", type=positive_int, default=6, help="Maximum demo screenshots/model calls")
    ap.add_argument("--timeout", type=positive_float, default=30, help="Model request timeout in seconds")
    ap.add_argument("--page-timeout", type=positive_float, default=10, help="Browser action/wait timeout in seconds")
    ap.add_argument("--scenario", choices=["success", "retry", "blocked", "exhausted", "pending"], default="retry")
    ap.add_argument("--url", help="Observe an existing HTTP(S) page once, without executing the demo")
    ap.add_argument("--goal", help="Task context for --url")
    ap.add_argument("--success", help="Completion statement to evaluate for --url")
    ap.add_argument("--wait-for", help="Wait for this visible CSS selector before checking --url")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if args.url:
        try:
            parsed_url = urlparse(args.url)
            valid_url = parsed_url.scheme in ("http", "https") and bool(parsed_url.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            ap.error("--url must be an HTTP(S) URL with a hostname")
    if args.url and not (args.success and args.success.strip()):
        ap.error("--url requires --success describing the expected visible result")
    if not args.url and any((args.goal, args.success, args.wait_for)):
        ap.error("--goal, --success and --wait-for require --url")
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        print("Install browser support: pip install -e '.[browser]'", file=sys.stderr)
        return 2

    try:
        recorder = Recorder(args.output)
    except OSError as exc:
        print(f"Cannot create artifact directory: {exc}", file=sys.stderr)
        return 2
    client = Client(args.base_url, model=args.model, timeout=args.timeout)
    settings = {"base_url": client.base_url, "mode": "check" if args.url else "demo",
                "scenario": None if args.url else args.scenario, "threshold": args.threshold,
                "min_confidence": args.min_confidence, "max_attempts": args.max_attempts,
                "max_observations": args.max_observations, "model": args.model,
                "channel": args.channel, "url": args.url, "goal": args.goal or (args.success if args.url else GOAL),
                "success": args.success if args.url else SUCCESS,
                "timeout": args.timeout, "page_timeout": args.page_timeout,
                "viewport": {"width": 1280, "height": 720}}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed, channel=args.channel)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
                page.set_default_timeout(args.page_timeout * 1000)
                try:
                    decision = (run_check if args.url else run_demo)(page, client, recorder, args)
                except (Exception, KeyboardInterrupt):
                    try:
                        page.screenshot(path=str(recorder.path / "error.png"), timeout=2000)
                    except PlaywrightError:
                        pass
                    raise
            finally:
                browser.close()
        recorder.finish(decision.action, decision.reason, settings=settings)
        return 0 if decision.action == "verified" else 1
    except HTTPError as exc:
        reason = f"Model server returned HTTP {exc.code}: {exc.read(2000).decode('utf-8', errors='replace')}"
    except URLError as exc:
        reason = (f"Cannot reach model server at {client.base_url}: {exc.reason}. "
                  "Start the model server in its inference environment, or select a running endpoint with --base-url.")
    except (TimeoutError, OSError, ValueError, KeyError, TypeError, PlaywrightError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        recorder.finish("interrupted", "Run interrupted.", settings=settings)
        return 130
    recorder.finish("error", reason, settings=settings)
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
