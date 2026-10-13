"""Routing quality evaluation on the labelled dataset, using the REAL model through Ollama.

Runs the production agent (same prompt, tool and settings) with an in-memory mailer and
fallback disabled, so every agent failure counts as an error. Writes a JSON file with raw
results and a Markdown report to `eval-results/`.

Usage (inside the dev container, e.g. `make eval`):
    python -m tests.eval.run_eval [--exclude-tags holdout] [--tags pl] [--limit 10]
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai import models

from ai_router.config import Settings
from ai_router.errors import AppError
from ai_router.llm.model import build_model
from ai_router.mail.sender import InMemoryEmailSender
from ai_router.routing.agent import build_agent
from ai_router.routing.departments import Department
from ai_router.routing.service import RoutingService

DATASET = Path(__file__).with_name("dataset.yaml")
ERROR = "ERROR"


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
    retries: int
    llm_requests: int
    seconds: float
    tags: list[str]
    error: str | None = None


def load_cases(path: Path, tags: set[str], exclude: set[str], limit: int | None) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [Case(**item) for item in raw]
    valid = {d.value for d in Department}
    for case in cases:
        unknown = {case.expected, *case.acceptable} - valid
        if unknown:
            raise ValueError(f"{case.id}: unknown department(s) {unknown}")
    if tags:
        cases = [c for c in cases if tags & set(c.tags)]
    if exclude:
        cases = [c for c in cases if not exclude & set(c.tags)]
    return cases[:limit] if limit else cases


async def run_case(service: RoutingService, case: Case) -> CaseResult:
    started = time.perf_counter()
    try:
        outcome = await service.route(
            sender_email="eval@example.com", message=case.message, request_id=f"eval-{case.id}"
        )
    except AppError as exc:
        return CaseResult(
            id=case.id,
            expected=case.expected,
            chosen=ERROR,
            strict_ok=False,
            lenient_ok=False,
            reason="",
            retries=0,
            llm_requests=0,
            seconds=time.perf_counter() - started,
            tags=case.tags,
            error=f"{type(exc).__name__}: {exc.detail}",
        )
    chosen = outcome.sent.department.value
    return CaseResult(
        id=case.id,
        expected=case.expected,
        chosen=chosen,
        strict_ok=chosen == case.expected,
        lenient_ok=chosen == case.expected or chosen in case.acceptable,
        reason=outcome.sent.reason,
        retries=outcome.diagnostics.retries,
        llm_requests=outcome.diagnostics.llm_requests,
        seconds=time.perf_counter() - started,
        tags=case.tags,
    )


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
    return ordered[index]


def short(address: str) -> str:
    return address.split("@")[0]


def build_report(results: list[CaseResult], model: str, cases: dict[str, Case]) -> str:
    total = len(results)
    strict = sum(r.strict_ok for r in results)
    lenient = sum(r.lenient_ok for r in results)
    errors = [r for r in results if r.chosen == ERROR]
    seconds = [r.seconds for r in results]
    mean_retries = statistics.mean(r.retries for r in results)
    mean_requests = statistics.mean(r.llm_requests for r in results)
    lines = [
        f"# Ewaluacja routingu - `{model}`",
        "",
        f"Data: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · przypadków: {total}",
        "",
        "| Metryka | Wartość |",
        "|---|---|",
        f"| Trafność (ścisła) | {percent(strict, total)} ({strict}/{total}) |",
        f"| Trafność (z alternatywami) | {percent(lenient, total)} ({lenient}/{total}) |",
        f"| Tool-compliance (agent wysłał mail sam) | {percent(total - len(errors), total)} |",
        f"| Średnia liczba ponowień | {mean_retries:.2f} |",
        f"| Średnia liczba zapytań do LLM | {mean_requests:.2f} |",
        f"| Czas p50 / p95 / max | {percentile(seconds, 0.5):.1f} s / "
        f"{percentile(seconds, 0.95):.1f} s / {max(seconds):.1f} s |",
        "",
        "## Trafność per dział (oczekiwany)",
        "",
        "| Dział | Trafność |",
        "|---|---|",
    ]
    by_department: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_department[r.expected].append(r)
    for department in Department:
        group = by_department.get(department.value, [])
        ok = sum(r.lenient_ok for r in group)
        lines.append(f"| `{department.value}` | {percent(ok, len(group))} ({ok}/{len(group)}) |")

    lines += ["", "## Trafność per tag", "", "| Tag | Trafność |", "|---|---|"]
    by_tag: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        for tag in r.tags:
            by_tag[tag].append(r)
    for tag in sorted(by_tag):
        group = by_tag[tag]
        ok = sum(r.lenient_ok for r in group)
        lines.append(f"| {tag} | {percent(ok, len(group))} ({ok}/{len(group)}) |")

    columns = [d.value for d in Department] + [ERROR]
    counts = Counter((r.expected, r.chosen) for r in results)
    lines += [
        "",
        "## Macierz pomyłek (wiersze: oczekiwany, kolumny: wybrany)",
        "",
        "| oczekiwany \\ wybrany | " + " | ".join(short(c) for c in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for expected in (d.value for d in Department):
        row = " | ".join(str(counts.get((expected, c), 0)) for c in columns)
        lines.append(f"| **{short(expected)}** | {row} |")

    misses = [r for r in results if not r.lenient_ok]
    lines += ["", f"## Błędy ({len(misses)})", ""]
    if not misses:
        lines.append("Brak.")
    for r in misses:
        detail = r.error or f"uzasadnienie modelu: {r.reason}"
        lines.append(
            f"- `{r.id}` «{cases[r.id].message[:90]}» → oczekiwano `{short(r.expected)}`, "
            f"wybrano `{short(r.chosen)}` ({detail})"
        )
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--tags", nargs="*", default=[], help="only cases with any of these tags")
    parser.add_argument("--exclude-tags", nargs="*", default=[], help="skip cases with these tags")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("eval-results"))
    args = parser.parse_args()

    models.ALLOW_MODEL_REQUESTS = True
    settings = Settings(routing_fallback_enabled=False, warmup_on_startup=False)
    cases = load_cases(args.dataset, set(args.tags), set(args.exclude_tags), args.limit)
    agent = build_agent(settings, build_model(settings))
    service = RoutingService(
        agent=agent,
        mailer=InMemoryEmailSender(),
        settings=settings,
        model_name=settings.ollama_model,
    )

    print(f"Evaluating {len(cases)} cases with {settings.ollama_model} ...", flush=True)
    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        result = await run_case(service, case)
        results.append(result)
        mark = "OK " if result.lenient_ok else "BAD"
        print(
            f"[{index:>3}/{len(cases)}] {mark} {case.id:<6} {result.seconds:5.1f}s "
            f"expected={short(case.expected):<15} chosen={short(result.chosen):<15} "
            f"{result.error or ''}",
            flush=True,
        )

    by_id = {c.id: c for c in cases}
    report = build_report(results, settings.ollama_model, by_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", settings.ollama_model)
    stem = f"{safe_model}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    raw: dict[str, Any] = {"model": settings.ollama_model, "results": [asdict(r) for r in results]}
    (args.output_dir / f"{stem}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    (args.output_dir / f"{stem}.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Saved: {args.output_dir / stem}.md / .json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
