"""Load test / benchmark of the running API (real model or LLM_BACKEND=fake).

Usage (inside the dev container): python -m tests.perf.loadtest [--concurrency 4] [--requests 20]
                                   [--async-mode] [--markdown]
Reports throughput, latency percentiles, HTTP statuses and routing modes. `--markdown` prints a
table row with a hardware description, ready to paste into the README.
"""

import argparse
import asyncio
import os
import platform
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8080")
MESSAGES = [
    "Nie działa mi komputer",
    "Chciałbym zgłosić urlop na jutro",
    "Serwer produkcyjny nie odpowiada, nikt w firmie nie może pracować",
    "Jakie szkolenia są dostępne w tym kwartale?",
    "Gdzie mogę zaparkować rower?",
]


def cpu_description() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return f"{line.split(':', 1)[1].strip()} x{os.cpu_count()}"
    except OSError:
        pass
    return f"{platform.processor() or platform.machine()} x{os.cpu_count()}"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0.0


async def one_request(
    client: httpx.AsyncClient, index: int, async_mode: bool
) -> tuple[int, float, str]:
    payload = {
        "email": f"load-{uuid.uuid4().hex[:8]}@example.com",
        "message": MESSAGES[index % len(MESSAGES)],
    }
    headers = {"Prefer": "respond-async"} if async_mode else {}
    started = time.perf_counter()
    response = await client.post("/api/v1/messages", json=payload, headers=headers)
    if async_mode and response.status_code == 202:
        location = response.headers["Location"]
        while True:
            status = (await client.get(location)).json()
            if status["status"] in ("routed", "failed"):
                routed_by = (status.get("result") or {}).get("routed_by", status["status"])
                return (
                    200 if status["status"] == "routed" else 500,
                    time.perf_counter() - started,
                    routed_by,
                )
            await asyncio.sleep(0.5)
    routed_by = response.json().get("routed_by", "-") if response.status_code == 200 else "-"
    return response.status_code, time.perf_counter() - started, routed_by


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--async-mode", action="store_true", help="use Prefer: respond-async")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=300) as client:
        info = (await client.get("/metrics")).text
        model = next((line for line in info.splitlines() if line.startswith("ai_router_info{")), "")

        async def bounded(i: int) -> tuple[int, float, str]:
            async with semaphore:
                return await one_request(client, i, args.async_mode)

        started = time.perf_counter()
        results = await asyncio.gather(*(bounded(i) for i in range(args.requests)))
        elapsed = time.perf_counter() - started

    latencies = [seconds for status, seconds, _ in results if status == 200]
    statuses = Counter(status for status, _, _ in results)
    modes = Counter(mode for _, _, mode in results)
    throughput = len(latencies) / elapsed * 60 if elapsed else 0.0
    print(f"API: {API_BASE_URL}  {model}")
    print(f"Requests: {args.requests}, concurrency: {args.concurrency}, async: {args.async_mode}")
    print(f"Elapsed: {elapsed:.1f} s, throughput: {throughput:.1f} routed/min")
    print(f"HTTP statuses: {dict(statuses)}   routed_by: {dict(modes)}")
    if latencies:
        print(
            f"Latency: p50 {percentile(latencies, 0.5):.1f} s, "
            f"p95 {percentile(latencies, 0.95):.1f} s, "
            f"max {max(latencies):.1f} s, mean {statistics.mean(latencies):.1f} s"
        )
    if args.markdown and latencies:
        print("\n| Sprzęt | Zgłoszeń | Równolegle | p50 | p95 | Przepustowość |")
        print("|---|---|---|---|---|---|")
        print(
            f"| {cpu_description()} | {args.requests} | {args.concurrency} | "
            f"{percentile(latencies, 0.5):.1f} s | {percentile(latencies, 0.95):.1f} s | "
            f"{throughput:.1f}/min |"
        )


if __name__ == "__main__":
    asyncio.run(main())
