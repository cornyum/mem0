"""Regression tests for the LOCOMO benchmark runner helpers.

The runner is a pure-stdlib standalone script under server/scripts, so it is
loaded by path rather than package import.
"""

import importlib.util
from pathlib import Path

_RUNNER = Path(__file__).resolve().parents[1] / "server" / "scripts" / "benchmarks" / "locomo" / "run.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("locomo_run", _RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compute_metrics_tolerates_no_answer_phases():
    runner = _load_runner()
    report = {
        "phases": {
            "ingest": {"done": True},
            "recall": {
                "done": True,
                "items": [
                    {"qid": "q1", "latency_ms": 100.0, "result_count": 50},
                    {"qid": "q2", "latency_ms": 200.0, "result_count": 40},
                ],
            },
            "answer": {},
            "judge": {},
        }
    }
    metrics = runner.compute_metrics(report)
    assert metrics["questions_scored"] == 0
    assert metrics["accuracy"] is None
    assert metrics["recall"]["questions"] == 2
    assert metrics["recall"]["p50_ms"] == 200.0
    assert metrics["recall"]["p95_ms"] == 200.0
    assert metrics["recall"]["avg_results"] == 45.0


def test_compute_metrics_judges_and_answers():
    runner = _load_runner()
    report = {
        "phases": {
            "ingest": {"done": True},
            "recall": {
                "items": [
                    {"qid": "q1", "latency_ms": 100.0, "result_count": 50, "category": 1, "sample_id": "conv-26"},
                    {"qid": "q2", "latency_ms": 200.0, "result_count": 40, "category": 2, "sample_id": "conv-26"},
                ]
            },
            "answer": {"errors": []},
            "judge": {
                "items": [
                    {"qid": "q1", "verdict": True},
                    {"qid": "q2", "verdict": False},
                ]
            },
        }
    }
    metrics = runner.compute_metrics(report)
    assert metrics["questions_scored"] == 2
    assert metrics["accuracy"] == 0.5
    assert metrics["per_category"]["multi_hop"]["total"] == 1
    assert metrics["per_category"]["temporal"]["total"] == 1
