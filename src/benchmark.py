"""Latency and output-quality benchmark across all models available on the account.

Discovers models via the Fireworks API, runs each on a fixed set of queries,
and prints a summary table sorted by median latency.

Usage:
    uv run benchmark
    # or
    python -m src.benchmark
"""

import os
import re
import statistics
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

from src.agent import DEFAULT_MODEL

load_dotenv()

# Models to always test regardless of what models.list() returns.
# Useful for serverless/public models not in the account's deployed list.
# DEFAULT_MODEL (qwen3-235b) is not available on this account — leave this empty.
EXTRA_MODELS: list[str] = []

# Queries that cover simple, medium, and complex SQL patterns.
PROBE_QUERIES = [
    "How many artists are there?",
    "What are the top 5 genres by number of tracks?",
    "Who are the top 3 customers by total spend?",
]

# A system prompt stripped down to the bare minimum for timing purposes
# (no schema = faster, but enough to get SQL output).
_SYSTEM = (
    "You are a SQL expert. Output ONLY a raw SQL SELECT query — "
    "no markdown, no code fences, no explanation."
)


def _has_preamble(text: str) -> bool:
    """Return True if the response has prose before the SQL."""
    first = text.strip().split("\n")[0].strip().upper()
    return not (first.startswith("SELECT") or first.startswith("WITH") or first.startswith("```"))


def _has_fencing(text: str) -> bool:
    return "```" in text


def _call(client: OpenAI, model: str, question: str) -> tuple[str, float, int, int]:
    """Call the model and return (response_text, latency_s, input_tokens, output_tokens)."""
    t0 = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": question},
        ],
        temperature=0,
        max_tokens=512,
    )
    latency = time.perf_counter() - t0
    content = response.choices[0].message.content or ""
    usage = response.usage
    input_tok = usage.prompt_tokens if usage else 0
    output_tok = usage.completion_tokens if usage else 0
    return content.strip(), latency, input_tok, output_tok


def _short_model_id(model_id: str) -> str:
    """Strip the common 'accounts/fireworks/models/' prefix for display."""
    return re.sub(r"^accounts/[^/]+/models/", "", model_id)


def main() -> None:
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        print("Error: FIREWORKS_API_KEY not set.")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url="https://api.fireworks.ai/inference/v1")

    print("Fetching available models...")
    # Exclude non-chat models (image generation, etc.) that would fail chat completions.
    _IMAGE_PATTERNS = ("flux", "stable-diffusion", "sdxl", "dall-e")
    discovered = [
        m.id for m in client.models.list()
        if not any(p in m.id.lower() for p in _IMAGE_PATTERNS)
    ]
    models = list(dict.fromkeys(EXTRA_MODELS + discovered))  # extras first, no duplicates
    if not models:
        print("No models returned by the API.")
        sys.exit(1)

    print(f"Found {len(discovered)} deployed model(s) + {len(EXTRA_MODELS)} extra. Running {len(PROBE_QUERIES)} probe queries each.\n")

    results = []

    for model_id in models:
        label = _short_model_id(model_id)
        latencies = []
        flags = set()
        total_input_tok = 0
        total_output_tok = 0
        error = None

        for i, question in enumerate(PROBE_QUERIES, 1):
            print(f"  [{label}] query {i}/{len(PROBE_QUERIES)}...", end=" ", flush=True)
            try:
                text, latency, input_tok, output_tok = _call(client, model_id, question)
                latencies.append(latency)
                total_input_tok += input_tok
                total_output_tok += output_tok
                if _has_fencing(text):
                    flags.add("fencing")
                if _has_preamble(text):
                    flags.add("preamble")
                print(f"{latency:.2f}s  ({input_tok} in / {output_tok} out tok)")
            except Exception as exc:
                error = str(exc)
                print(f"ERROR: {exc}")
                break

        n = len(latencies)
        if latencies:
            median_s = statistics.median(latencies)
            min_s = min(latencies)
        else:
            median_s = min_s = float("inf")

        results.append(
            {
                "model": label,
                "median_s": median_s,
                "min_s": min_s,
                "n": n,
                "avg_input_tok": round(total_input_tok / n) if n else 0,
                "avg_output_tok": round(total_output_tok / n) if n else 0,
                "flags": ", ".join(sorted(flags)) if flags else "clean",
                "error": error,
            }
        )

        time.sleep(1)  # avoid rate limits between models

    # Sort by median latency, drop errors from display.
    results.sort(key=lambda r: r["median_s"])
    passed = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]

    if failed:
        print(f"Skipped {len(failed)} model(s) with errors: {', '.join(r['model'] for r in failed)}\n")

    if not passed:
        print("No models completed successfully.")
        return

    # Print table.
    col_model = max(len(r["model"]) for r in passed)
    col_model = max(col_model, 5)

    print(f"\n{'='*100}")
    print(
        f"{'Model':<{col_model}}  {'Median':>8}  {'Min':>6}  {'Avg in tok':>10}  {'Avg out tok':>11}  Output"
    )
    print(f"{'-'*col_model}  {'-'*8}  {'-'*6}  {'-'*10}  {'-'*11}  {'-'*20}")

    for r in passed:
        row = (
            f"{r['model']:<{col_model}}  {r['median_s']:.2f}s  {r['min_s']:.2f}s  "
            f"{r['avg_input_tok']:>10}  {r['avg_output_tok']:>11}  {r['flags']}"
        )
        print(row)

    print(f"{'='*100}")
    print("(Avg tok = average across probe queries. Multiply by Fireworks $/M tok for cost/query.)")
    print(f"\nTarget P50: <3.00s")

    passing = [r for r in passed if r["median_s"] < 3.0]
    if passing:
        best = passing[0]
        print(f"Models meeting target: {', '.join(r['model'] for r in passing)}")
        print(f"Recommended: {best['model']} (median {best['median_s']:.2f}s, output: {best['flags']})")
    else:
        print("No models met the <3s P50 target.")


if __name__ == "__main__":
    main()
