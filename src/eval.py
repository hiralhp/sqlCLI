"""Evaluation script — runs all dev questions and writes data/dev_answers.json.

Usage:
    uv run eval
    # or
    python -m src.eval
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.agent import TextToSQLAgent
from src.utils import load_db

QUESTIONS_PATH = Path("data/dev_questions.json")
OUTPUT_PATH = Path("data/dev_answers.json")


def _summarize(results: list[dict], max_rows: int = 5) -> str:
    """Build a compact human-readable summary of query results."""
    if not results:
        return "(no results)"
    cols = list(results[0].keys())
    rows = results[:max_rows]
    parts = [", ".join(f"{c}: {row.get(c, '')}" for c in cols) for row in rows]
    summary = "; ".join(parts)
    if len(results) > max_rows:
        summary += f" ... (+{len(results) - max_rows} more rows)"
    return summary


def main() -> None:
    db_path = os.environ.get("CHINOOK_DB_PATH", "data/Chinook.db")

    try:
        conn = load_db(db_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    try:
        agent = TextToSQLAgent(conn)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    with open(QUESTIONS_PATH) as f:
        questions: list[dict] = json.load(f)

    answers: dict[str, dict] = {}
    total_latency = 0.0
    n_ok = 0

    print(f"Running {len(questions)} questions against {db_path}\n")

    for q in questions:
        qid: str = q["id"]
        question: str = q["question"]
        tier: int = q.get("tier", 0)

        print(f"[{qid}] (tier {tier}) {question}")

        t0 = time.perf_counter()
        try:
            sql, results = agent.ask(question)
            latency = time.perf_counter() - t0
            answer = _summarize(results)
            answers[qid] = {
                "sql": sql,
                "answer": answer,
                "latency_s": round(latency, 3),
            }
            n_ok += 1
            preview = answer[:90] + ("..." if len(answer) > 90 else "")
            print(f"  OK  {latency:.2f}s  {preview}")
        except Exception as exc:
            latency = time.perf_counter() - t0
            answers[qid] = {
                "sql": "",
                "answer": f"ERROR: {exc}",
                "latency_s": round(latency, 3),
            }
            print(f"  FAIL {latency:.2f}s  {exc}")

        total_latency += latency
        # Reset history between questions — each is evaluated independently.
        agent.reset_history()
        # Brief pause to stay within API rate limits.
        time.sleep(1)

    # Write results (matching the dev_answers_example.json structure).
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        # Write only the fields required by the answer format.
        slim = {qid: {"sql": v["sql"], "answer": v["answer"]} for qid, v in answers.items()}
        json.dump(slim, f, indent=2)

    # Also write a detailed version with latency alongside the slim one.
    detailed_path = OUTPUT_PATH.with_name("dev_answers_detailed.json")
    with open(detailed_path, "w") as f:
        json.dump(answers, f, indent=2)

    n = len(questions)
    avg = total_latency / n if n else 0
    print(f"\n{'='*60}")
    print(f"Results:  {n_ok}/{n} succeeded")
    print(f"Latency:  total={total_latency:.2f}s  avg={avg:.2f}s/question")
    print(f"Output:   {OUTPUT_PATH}  (slim)")
    print(f"          {detailed_path}  (with latency)")


if __name__ == "__main__":
    main()
