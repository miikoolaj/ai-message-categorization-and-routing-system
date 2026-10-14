"""The evaluation dataset and the pure helpers of the evaluation script (no LLM involved)."""

from collections import Counter
from pathlib import Path

import pytest

from ai_router.routing.policy import load_policy
from tests.eval.run_eval import (
    DATASET,
    CaseResult,
    guess_language,
    load_cases,
    normalize,
    summarize,
    wilson_interval,
)

POLICY = load_policy()


def test_dataset_is_valid_and_disjoint_from_policy_examples() -> None:
    cases = load_cases(DATASET, set(), set(), None, POLICY)
    ids = Counter(c.id for c in cases)
    assert [i for i, n in ids.items() if n > 1] == []
    assert len(cases) >= 150
    assert not [c.id for c in cases if "in-prompt" in c.tags], "few-shot examples leaked into eval"
    holdout = sum("holdout" in c.tags for c in cases)
    assert 0.2 <= holdout / len(cases) <= 0.4
    per_department = Counter(c.expected for c in cases)
    assert set(per_department) == set(POLICY.addresses)
    assert min(per_department.values()) >= 20


def test_overlap_with_policy_examples_is_tagged(tmp_path: Path) -> None:
    example = POLICY.departments[0].examples[0]
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        f'- id: x-1\n  message: "{example.upper()}"\n  expected: {POLICY.departments[0].address}\n'
        "  tags: [pl]\n",
        encoding="utf-8",
    )
    [case] = load_cases(dataset, set(), set(), None, POLICY)
    assert "in-prompt" in case.tags


def test_unknown_department_in_dataset_is_rejected(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        "- id: x-1\n  message: hi\n  expected: ceo@example.com\n  tags: []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown department"):
        load_cases(dataset, set(), set(), None, POLICY)


def test_tag_filters() -> None:
    holdout = load_cases(DATASET, {"holdout"}, set(), None, POLICY)
    tuning = load_cases(DATASET, set(), {"holdout"}, None, POLICY)
    assert holdout and tuning
    assert {c.id for c in holdout}.isdisjoint({c.id for c in tuning})
    assert len(load_cases(DATASET, set(), set(), 5, POLICY)) == 5


@pytest.mark.parametrize(
    ("successes", "total", "low", "high"),
    [(0, 0, 0.0, 0.0), (10, 10, 0.72, 1.0), (90, 100, 0.825, 0.945), (0, 10, 0.0, 0.28)],
)
def test_wilson_interval(successes: int, total: int, low: float, high: float) -> None:
    lo, hi = wilson_interval(successes, total)
    assert lo == pytest.approx(low, abs=0.01)
    assert hi == pytest.approx(high, abs=0.01)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Nie działa mi drukarka przy biurku", "pl"),
        ("My laptop is not working, please help", "en"),
        ("Ich möchte nächste Woche Urlaub nehmen", "de"),
        ("Не працює мій ноутбук", "uk"),  # noqa: RUF001 - Ukrainian on purpose
        ("1234", "unknown"),
    ],
)
def test_guess_language(text: str, language: str) -> None:
    assert guess_language(text) == language


def test_normalize_ignores_case_and_punctuation() -> None:
    assert normalize("Kto zamawia KWIATY do recepcji?!") == "kto zamawia kwiaty do recepcji"


def _result(
    case_id: str, *, ok: bool, chosen: str = "it@example.com", **kwargs: object
) -> CaseResult:
    defaults: dict[str, object] = {
        "id": case_id,
        "expected": "it@example.com",
        "chosen": chosen,
        "strict_ok": ok,
        "lenient_ok": ok,
        "reason": "r",
        "subject": "s",
        "confidence": 0.9,
        "needs_review": False,
        "duplicates": 0,
        "retries": 0,
        "llm_requests": 2,
        "seconds": 1.0,
        "tags": [],
    }
    defaults.update(kwargs)
    return CaseResult(**defaults)  # type: ignore[arg-type]


def test_summary_excludes_leaked_cases_from_clean_accuracy() -> None:
    results = [
        _result("a", ok=True),
        _result("b", ok=False, chosen="ERROR"),
        _result("c", ok=False, tags=["in-prompt"]),
        _result("d", ok=True, duplicates=1),
    ]
    summary = summarize(results)
    assert summary.accuracy == pytest.approx(0.5)
    assert summary.accuracy_no_leak == pytest.approx(2 / 3)
    assert summary.tool_compliance == pytest.approx(0.75)
    assert summary.duplicate_rate == pytest.approx(0.25)


def test_holdout_errors_are_hidden_unless_revealed() -> None:
    from tests.eval.run_eval import Case, build_report

    cases = {
        "a": Case(id="a", message="Tajny przypadek kontrolny", expected="it@example.com"),
        "b": Case(id="b", message="Przypadek strojeniowy", expected="it@example.com"),
    }
    results = [
        _result("a", ok=False, chosen="other@example.com", tags=["holdout"], reason="sekret"),
        _result("b", ok=False, chosen="other@example.com", reason="widoczne"),
    ]
    meta = {"model": "m", "agent_mode": "tool", "prompt_version": "p", "policy_version": "v"}
    hidden = build_report(results, {**meta, "subject_language": "pl"}, cases)
    assert "Tajny przypadek" not in hidden and "sekret" not in hidden
    assert "Przypadek strojeniowy" in hidden and "widoczne" in hidden
    assert "po polsku" in hidden  # language metric follows MAIL_SUBJECT_LANGUAGE
    revealed = build_report(results, meta, cases, reveal_holdout=True)
    assert "Tajny przypadek" in revealed
