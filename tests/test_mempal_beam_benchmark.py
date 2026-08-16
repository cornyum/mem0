"""Regression tests for the cn-Mem-PAL v1 and BEAM v1 benchmark runners.

Both runners are stdlib-only standalone scripts under server/scripts, so they
are loaded by path rather than package import.
"""

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "server" / "scripts" / "benchmarks"
_MEMPAL = _ROOT / "mempal" / "run.py"
_BEAM = _ROOT / "beam" / "run.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- cn-Mem-PAL ---------------------------------------------------------------


def test_mempal_build_sample_messages_prefixes_log_timestamps():
    runner = _load(_MEMPAL, "mempal_run")
    sample = {
        "logs": [{"timestamp": "2024-01-01 09:15", "content": "用户搜索了路线"}],
        "dialogue": {
            "turn_1": {"user": {"content": "你好"}, "assistant": {"content": "你好"}},
            "turn_10": {"user": {"content": "再见"}, "assistant": {"content": "再见"}},
        },
    }
    messages = runner.build_sample_messages(sample)
    assert len(messages) == 5
    assert messages[0] == {"role": "user", "content": "[log 2024-01-01 09:15] 用户搜索了路线"}
    assert [m["content"] for m in messages[1:]] == ["你好", "你好", "再见", "再见"]


def test_mempal_sorted_topic_ids_numeric_order():
    runner = _load(_MEMPAL, "mempal_run")
    assert runner.sorted_topic_ids({"topic-10": 1, "topic-2": 2}) == ["topic-2", "topic-10"]


def test_mempal_compute_metrics_requirement_and_solution():
    runner = _load(_MEMPAL, "mempal_run")
    report = {
        "phases": {
            "ingest": {"samples": {}},
            "recall": {
                "items": [
                    {
                        "qid": "0000|0000_sample30|topic-1",
                        "user_id": "0000",
                        "user_query": "q",
                        "requirement": "r",
                        "implicit_needs": ["a", "b"],
                        "candidates": [
                            {"id": "S1", "feedback": "pos"},
                            {"id": "S2", "feedback": "neg"},
                        ],
                        "req": {"latency_ms": 100.0, "result_count": 50},
                        "sol": {"latency_ms": 90.0, "result_count": 50},
                    }
                ]
            },
            "requirement_answer": {"items": [], "errors": []},
            "requirement_judge": {
                "items": [{"qid": "0000|0000_sample30|topic-1", "score": 2.0}],
                "errors": [],
            },
            "solution_answer": {
                "items": [
                    {
                        "qid": "0000|0000_sample30|topic-1",
                        "selected_solutions": ["S1", "S2"],
                    }
                ],
                "errors": [],
            },
        }
    }
    metrics = runner.compute_metrics(report)
    assert metrics["requirement"]["score_100"] == 100.0
    assert metrics["requirement"]["full_2_rate"] == 1.0
    assert metrics["solution"]["score_100"] == 0.0  # pos - neg = 0
    assert metrics["solution"]["exact_pos_rate"] == 0.0
    assert metrics["solution"]["ge_1_pos_rate"] == 1.0
    assert metrics["per_user"]["0000"]["requirement_score_100"] == 100.0


def test_mempal_parse_args_smoke_scope_flags():
    runner = _load(_MEMPAL, "mempal_run")
    args = runner.parse_args(
        [
            "--no-auth",
            "--no-answer",
            "--users",
            "0000",
            "--history-limit",
            "2",
            "--query-limit",
            "1",
            "--topic-limit",
            "1",
            "--tenant",
            "bench-smoke-mempal",
            "--out",
            "/tmp/mempal-smoke-test.json",
        ]
    )
    assert args.users == "0000"
    assert (args.history_limit, args.query_limit, args.topic_limit) == (2, 1, 1)
    assert args.tenant == "bench-smoke-mempal"

    with pytest.raises(SystemExit):
        runner.parse_args(["--no-auth", "--no-answer", "--skip-ingest"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--no-auth", "--no-answer", "--skip-ingest", "--ingest-tag", "t", "--reset"])


# -- BEAM ---------------------------------------------------------------------


def test_beam_parse_time_anchor_and_conversation_indices():
    runner = _load(_BEAM, "beam_run")
    assert runner.parse_time_anchor("March-15-2024") == "2024-03-15"
    assert runner.parse_time_anchor("April 5, 2024") == "2024-04-05"
    assert runner.parse_time_anchor(None) is None
    assert runner.parse_conversation_indices("0-2,5", 20) == [0, 1, 2, 5]
    assert runner.parse_conversation_indices(None, 20) == list(range(20))


def test_beam_collect_questions_uses_question_key_and_filters():
    runner = _load(_BEAM, "beam_run")
    dataset = [
        {
            "conversation_id": "1",
            "probing_questions": {
                "information_extraction": [
                    {"question": "When does my sprint end?", "rubric": ["March 29"], "difficulty": "easy"},
                    {"question": "Second question?", "rubric": ["gold"]},
                ],
                "summarization": [{"question": "Summarize the project.", "rubric": ["summary"]}],
            },
        }
    ]
    args = runner.parse_args(
        ["--no-auth", "--no-answer", "--conversations", "0", "--types", "information_extraction", "--question-limit", "1"]
    )
    questions = runner.collect_questions(dataset, [0], args, "bench-smoke-beam")
    assert len(questions) == 1
    assert questions[0]["question"] == "When does my sprint end?"
    assert questions[0]["rubric"] == ["March 29"]
    assert questions[0]["scope"] == {"tenant_id": "bench-smoke-beam", "user_id": "beam_100k_1"}


def test_beam_compute_metrics_nugget_scoring():
    runner = _load(_BEAM, "beam_run")
    report = {
        "phases": {
            "ingest": {"samples": []},
            "recall": {
                "items": [
                    {
                        "qid": "1_q0_information_extraction",
                        "question_type": "information_extraction",
                        "question": "q",
                        "rubric": ["n1", "n2"],
                        "latency_ms": 100.0,
                        "result_count": 50,
                    }
                ]
            },
            "answer": {"items": [{"qid": "1_q0_information_extraction", "prediction": "a"}], "errors": []},
            "judge": {
                "items": [
                    {"qid": "1_q0_information_extraction", "nugget_index": 0, "score": 1.0},
                    {"qid": "1_q0_information_extraction", "nugget_index": 1, "score": 0.5},
                ],
                "errors": [],
            },
        }
    }
    metrics = runner.compute_metrics(report)
    assert metrics["questions"] == 1
    assert metrics["accuracy"] == 1.0  # mean 0.75 >= 0.5
    assert metrics["avg_score"] == 0.75
    assert metrics["by_type"]["information_extraction"]["accuracy"] == 1.0
