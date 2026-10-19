"""Routing quality evaluation on the labelled dataset, using the REAL model through Ollama.

Runs the production agent (same policy, prompt, tool and settings - including AGENT_MODE from the
environment) with an in-memory gateway and fallback disabled, so every agent failure counts as an
error. Writes raw results (JSON) and a Markdown report to `eval-results/`.

Usage (inside the dev container, e.g. `make eval`):
    python -m tests.eval.run_eval [--exclude-tags holdout] [--tags pl] [--limit 10]
        [--min-accuracy 0.9 --min-tool-compliance 0.98 --max-duplicate-rate 0.05]
Exit code 1 when a quality gate fails (used by the model-eval CI workflow).
"""

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic_ai import models

from ai_router.config import Settings
from ai_router.errors import AppError
from ai_router.llm.model import build_model
from ai_router.mail.composer import MailSettings
from ai_router.mail.gateway import InMemoryMailGateway
from ai_router.routing.agent import build_agent
from ai_router.routing.deps import AgentRuntime
from ai_router.routing.policy import RoutingPolicy, load_policy
from ai_router.routing.service import RoutingRequest, RoutingService

DATASET = Path(__file__).with_name("dataset.yaml")
ERROR = "ERROR"
_SUBJECT_PREFIX = re.compile(r"^\[[^\]]*\]\s*\[[^\]]*\]\s*(\[Do weryfikacji\]\s*)?")
_CYRILLIC = "\u0430-\u044f\u0456\u0457\u0454\u0491"  # Ukrainian letters (escaped for linters)
_WORD = re.compile(rf"[a-ząćęłńóśźżäöüß{_CYRILLIC}]+", re.IGNORECASE)
_STOPWORD_TEXT = {
    "pl": (
        "nie się jest mi na do w z że jak czy proszę mój moja dla od po jutro chcę mam jestem "
        "problem zgłoszenie"
    ),
    "en": (
        "the is my and for not of in please with have from request issue cannot can you your "
        "it this that"
    ),
    "de": "der die das ist nicht ich und mein meine mit zu von für bitte ein eine urlaub",
}
_STOPWORDS = {lang: set(words.split()) for lang, words in _STOPWORD_TEXT.items()}


@dataclass
class Case:
    id: str
    message: str
    expected: str
    acceptable: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    expected: str
    chosen: str
    strict_ok: bool
    lenient_ok: bool
    reason: str
    subject: str
    confidence: float | None
    needs_review: bool
    duplicates: int
    retries: int
    llm_requests: int
    seconds: float
    tags: list[str]
    error: str | None = None


def normalize(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def guess_language(text: str) -> str:
    """Tiny heuristic (stop words + diacritics) - indicative only."""
    lowered = text.lower()
    if re.search(f"[{_CYRILLIC}]", lowered):
        return "uk"
    words = _WORD.findall(lowered)
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    if re.search(r"[ąćęłńśźż]", lowered):
        scores["pl"] += 2
    if re.search(r"[äöüß]", lowered):
        scores["de"] += 2
    best = max(scores, key=lambda lang: scores[lang])
    return best if scores[best] > 0 else "unknown"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def load_cases(
    path: Path, tags: set[str], exclude: set[str], limit: int | None, policy: RoutingPolicy
) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [Case(**item) for item in raw]
    valid = set(policy.addresses)
    examples = {normalize(e) for d in policy.departments for e in d.examples}
    for case in cases:
        unknown = {case.expected, *case.acceptable} - valid
        if unknown:
            raise ValueError(f"{case.id}: unknown department(s) {unknown}")
        if normalize(case.message) in examples and "in-prompt" not in case.tags:
            case.tags.append("in-prompt")  # leakage: few-shot example evaluated as a test case
    if tags:
        cases = [c for c in cases if tags & set(c.tags)]
    if exclude:
        cases = [c for c in cases if not exclude & set(c.tags)]
    return cases[:limit] if limit else cases


async def run_case(service: RoutingService, case: Case, index: int) -> CaseResult:
    started = time.perf_counter()
    request = RoutingRequest(
        ticket_id=f"{index:032x}",
        ticket_code=f"AIR-EVAL{index:04d}",
        request_id=f"eval-{case.id}",
        sender_email="eval@example.com",
        sender_verification="none",
        message=case.message,
    )
    try:
        outcome = await service.route(request)
    except AppError as exc:
        return CaseResult(
            id=case.id,
            expected=case.expected,
            chosen=ERROR,
            strict_ok=False,
            lenient_ok=False,
            reason="",
            subject="",
            confidence=None,
            needs_review=False,
            duplicates=0,
            retries=0,
            llm_requests=0,
            seconds=time.perf_counter() - started,
            tags=case.tags,
            error=f"{type(exc).__name__}: {exc.detail}",
        )
    sent = outcome.sent
    chosen = sent.chosen_department
    return CaseResult(
        id=case.id,
        expected=case.expected,
        chosen=chosen,
        strict_ok=chosen == case.expected,
        lenient_ok=chosen == case.expected or chosen in case.acceptable,
        reason=sent.reason,
        subject=_SUBJECT_PREFIX.sub("", sent.subject),
        confidence=sent.confidence,
        needs_review=sent.needs_review,
        duplicates=outcome.diagnostics.tool_duplicates,
        retries=outcome.diagnostics.retries,
        llm_requests=outcome.diagnostics.llm_requests,
        seconds=time.perf_counter() - started,
        tags=case.tags,
    )


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def share(flags: list[bool]) -> str:
    """`93.3% (14/15)` - the share of True values with the counts."""
    return f"{percent(sum(flags), len(flags))} ({sum(flags)}/{len(flags)})"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0.0


def mean(values: list[float] | list[int]) -> float:
    return statistics.mean(values) if values else 0.0


def short(address: str) -> str:
    return address.split("@")[0]


@dataclass
class Summary:
    total: int
    accuracy: float
    accuracy_no_leak: float
    tool_compliance: float
    duplicate_rate: float


def summarize(results: list[CaseResult]) -> Summary:
    total = len(results)
    clean = [r for r in results if "in-prompt" not in r.tags]
    return Summary(
        total=total,
        accuracy=sum(r.lenient_ok for r in results) / total if total else 0.0,
        accuracy_no_leak=sum(r.lenient_ok for r in clean) / len(clean) if clean else 0.0,
        tool_compliance=sum(r.chosen != ERROR for r in results) / total if total else 0.0,
        duplicate_rate=sum(r.duplicates > 0 for r in results) / total if total else 0.0,
    )


def _grouped_rows(groups: dict[str, list[CaseResult]], label: str) -> list[str]:
    return [
        f"| {label.format(key)} | {share([r.lenient_ok for r in groups[key]])} |"
        for key in sorted(groups)
    ]


def build_report(
    results: list[CaseResult],
    meta: dict[str, str],
    cases: dict[str, Case],
    *,
    reveal_holdout: bool = False,
) -> str:
    total = len(results)
    clean = [r for r in results if "in-prompt" not in r.tags]
    clean_ok = sum(r.lenient_ok for r in clean)
    routed = [r for r in results if r.chosen != ERROR]
    seconds = [r.seconds for r in results]
    low, high = wilson_interval(clean_ok, len(clean))
    lines = [
        f"# Ewaluacja routingu - `{meta['model']}`",
        "",
        f"Data: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · przypadków: {total} · "
        f"tryb agenta: `{meta['agent_mode']}` · prompt: `{meta['prompt_version']}` · "
        f"polityka: `{meta['policy_version']}` · "
        f"język tematu: `{meta.get('subject_language', '?')}`",
        "",
        "| Metryka | Wartość |",
        "|---|---|",
        f"| Trafność (ścisła) | {share([r.strict_ok for r in results])} |",
        f"| Trafność (z alternatywami) | {share([r.lenient_ok for r in results])} |",
        f"| Trafność bez przecieku z promptu | {share([r.lenient_ok for r in clean])}, "
        f"95% CI Wilsona: {100 * low:.1f}-{100 * high:.1f}% |",
        f"| Przypadki obecne w prompcie (`in-prompt`) | {total - len(clean)} |",
        f"| Tool-compliance (wysłał agent) | {share([r.chosen != ERROR for r in results])} |",
        f"| Duplikaty wywołań narzędzia | {share([r.duplicates > 0 for r in results])} |",
        f"| Decyzje poniżej progu pewności | {share([r.needs_review for r in results])} |",
        f"| Średnio ponowień / zapytań do LLM | {mean([r.retries for r in results]):.2f} / "
        f"{mean([r.llm_requests for r in results]):.2f} |",
        f"| Czas p50 / p95 / max | {percentile(seconds, 0.5):.1f} s / "
        f"{percentile(seconds, 0.95):.1f} s / {max(seconds, default=0.0):.1f} s |",
        "",
        "## Kalibracja pewności",
        "",
        "| Pewność modelu | Trafność |",
        "|---|---|",
    ]
    for label, lo, hi in (("< 0.5", 0.0, 0.5), ("0.5 - 0.8", 0.5, 0.8), (">= 0.8", 0.8, 1.01)):
        group = [
            r.lenient_ok for r in results if r.confidence is not None and lo <= r.confidence < hi
        ]
        lines.append(f"| {label} | {share(group)} |")

    by_department: dict[str, list[CaseResult]] = defaultdict(list)
    by_tag: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_department[r.expected].append(r)
        for tag in r.tags:
            by_tag[tag].append(r)
    lines += ["", "## Trafność per dział (oczekiwany)", "", "| Dział | Trafność |", "|---|---|"]
    lines += _grouped_rows(by_department, "`{}`")
    lines += ["", "## Trafność per tag", "", "| Tag | Trafność |", "|---|---|"]
    lines += _grouped_rows(by_tag, "{}")

    subject_words = [len(r.subject.split()) for r in routed]
    # MAIL_SUBJECT_LANGUAGE=pl: always Polish; auto: the language of the message.
    fixed_language = "pl" if meta.get("subject_language") == "pl" else None
    languages = [
        (
            fixed_language or guess_language(cases[r.id].message),
            guess_language(f"{r.subject} {r.reason}"),
        )
        for r in routed
    ]
    comparable = [a == b for a, b in languages if "unknown" not in (a, b)]
    language_label = (
        "Temat i uzasadnienie po polsku (heurystyka)"
        if fixed_language
        else "Język tematu i uzasadnienia = język wiadomości (heurystyka)"
    )
    lines += [
        "",
        "## Jakość tekstów generowanych przez model",
        "",
        "| Metryka | Wartość |",
        "|---|---|",
        f"| Średnia długość tematu | {mean(subject_words):.1f} słów |",
        f"| Tematy w zakresie 2-12 słów | {share([2 <= n <= 12 for n in subject_words])} |",
        f"| {language_label} | {share(comparable)} |",
        f"| Niepuste uzasadnienie | {share([bool(r.reason.strip()) for r in routed])} |",
    ]

    columns = [*sorted({r.expected for r in results}), ERROR]
    counts = Counter((r.expected, r.chosen) for r in results)
    lines += [
        "",
        "## Macierz pomyłek (wiersze: oczekiwany, kolumny: wybrany)",
        "",
        "| oczekiwany \\ wybrany | " + " | ".join(short(c) for c in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for expected in columns[:-1]:
        row = " | ".join(str(counts.get((expected, c), 0)) for c in columns)
        lines.append(f"| **{short(expected)}** | {row} |")

    misses = [r for r in results if not r.lenient_ok]
    lines += ["", f"## Błędy ({len(misses)})", ""]
    if not misses:
        lines.append("Brak.")
    for r in misses:
        if "holdout" in r.tags and not reveal_holdout:
            # Hidden on purpose: holdout errors must not steer prompt/policy tuning.
            lines.append(f"- `{r.id}` (holdout - szczegóły ukryte, `--reveal-holdout` je pokaże)")
            continue
        detail = r.error or f"uzasadnienie modelu: {r.reason}"
        lines.append(
            f"- `{r.id}` «{cases[r.id].message[:90]}» → oczekiwano `{short(r.expected)}`, "
            f"wybrano `{short(r.chosen)}` ({detail})"
        )
    return "\n".join(lines) + "\n"


async def routed_total(client: httpx.AsyncClient) -> float | None:
    """Messages routed by the running API (to warn about concurrent traffic)."""
    try:
        text = (await client.get("/metrics")).text
    except httpx.HTTPError:
        return None
    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith("ai_router_routed_total{")
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--tags", nargs="*", default=[], help="only cases with any of these tags")
    parser.add_argument("--exclude-tags", nargs="*", default=[], help="skip cases with these tags")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("eval-results"))
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument("--min-tool-compliance", type=float, default=None)
    parser.add_argument("--max-duplicate-rate", type=float, default=None)
    parser.add_argument(
        "--reveal-holdout",
        action="store_true",
        help="show messages and reasons of holdout errors (only after tuning is finished)",
    )
    args = parser.parse_args()

    models.ALLOW_MODEL_REQUESTS = True
    settings = Settings(routing_fallback_enabled=False, warmup_on_startup=False)
    policy = load_policy(settings.routing_policy_file)
    cases = load_cases(args.dataset, set(args.tags), set(args.exclude_tags), args.limit, policy)
    handle = build_model(settings, policy)
    routing_agent = build_agent(settings, handle.model, policy)
    runtime = AgentRuntime(
        policy=policy,
        mail_settings=MailSettings.from_settings(settings),
        model_name=handle.name,
        prompt_version=routing_agent.prompt_version,
        confidence_threshold=settings.confidence_threshold,
        low_confidence_action="flag",  # evaluate the agent's own choice
    )
    service = RoutingService(
        routing_agent=routing_agent,
        gateway=InMemoryMailGateway(),
        runtime=runtime,
        settings=settings,
    )
    meta = {
        "model": handle.name,
        "agent_mode": routing_agent.mode,
        "prompt_version": routing_agent.prompt_version,
        "policy_version": policy.version,
        "subject_language": settings.mail_subject_language,
    }
    leaked = sum("in-prompt" in c.tags for c in cases)
    print(f"Evaluating {len(cases)} cases ({leaked} in-prompt) with {meta} ...", flush=True)

    api = httpx.AsyncClient(base_url=os.environ.get("API_BASE_URL", "http://api:8000"), timeout=5)
    traffic_before = await routed_total(api)
    results: list[CaseResult] = []
    try:
        for index, case in enumerate(cases, start=1):
            result = await run_case(service, case, index)
            results.append(result)
            mark = "OK " if result.lenient_ok else "BAD"
            hidden = "holdout" in case.tags and not args.reveal_holdout
            decision = (
                "(holdout)"
                if hidden
                else f"expected={short(case.expected):<15} chosen={short(result.chosen):<15}"
            )
            print(
                f"[{index:>3}/{len(cases)}] {mark} {case.id:<7} {result.seconds:5.1f}s {decision} "
                f"conf={result.confidence if result.confidence is not None else '-'} "
                f"{'DUP ' if result.duplicates else ''}{result.error or ''}",
                flush=True,
            )
    finally:
        await handle.close()
    traffic_after = await routed_total(api)
    await api.aclose()
    if traffic_before is not None and traffic_after is not None and traffic_after > traffic_before:
        print(
            f"WARNING: the API routed {int(traffic_after - traffic_before)} message(s) during the "
            "evaluation - latency numbers may be distorted.",
            flush=True,
        )

    by_id = {c.id: c for c in cases}
    report = build_report(results, meta, by_id, reveal_holdout=args.reveal_holdout)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", handle.name)
    stem = f"{safe_model}-{routing_agent.mode}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    raw: dict[str, Any] = {**meta, "results": [asdict(r) for r in results]}
    (args.output_dir / f"{stem}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    (args.output_dir / f"{stem}.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Saved: {args.output_dir / stem}.md / .json")

    summary = summarize(results)
    failures = []
    if args.min_accuracy is not None and summary.accuracy_no_leak < args.min_accuracy:
        failures.append(f"accuracy {summary.accuracy_no_leak:.3f} < {args.min_accuracy}")
    if args.min_tool_compliance is not None and summary.tool_compliance < args.min_tool_compliance:
        failures.append(
            f"tool compliance {summary.tool_compliance:.3f} < {args.min_tool_compliance}"
        )
    if args.max_duplicate_rate is not None and summary.duplicate_rate > args.max_duplicate_rate:
        failures.append(f"duplicate rate {summary.duplicate_rate:.3f} > {args.max_duplicate_rate}")
    if failures:
        print("QUALITY GATE FAILED: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
