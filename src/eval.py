"""Evaluation script — runs questions through the agent and writes answers.

Usage:
    uv run eval                                          # dev questions (default)
    uv run eval -- --questions data/live_questions.json  # live questions
    python -m src.eval --questions data/live_questions.json --output data/live_answers.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.agent import TextToSQLAgent
from src.utils import load_db

_DEFAULT_QUESTIONS = Path("data/dev_questions.json")
_DEFAULT_OUTPUT = Path("data/dev_answers.json")


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
    parser = argparse.ArgumentParser(description="Run eval questions through the SQL agent.")
    parser.add_argument("--questions", type=Path, default=_DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    questions_path: Path = args.questions
    output_path: Path = args.output or questions_path.with_name(
        questions_path.stem.replace("_questions", "_answers") + ".json"
    )

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

    with open(questions_path) as f:
        questions: list[dict] = json.load(f)

    answers: dict[str, dict] = {}
    total_latency = 0.0
    n_ok = 0

    print(f"Questions: {questions_path}")
    print(f"Output:    {output_path}")
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        slim = {qid: {"sql": v["sql"], "answer": v["answer"]} for qid, v in answers.items()}
        json.dump(slim, f, indent=2)

    # Also write a detailed version with latency.
    detailed_path = output_path.with_stem(output_path.stem + "_detailed")
    with open(detailed_path, "w") as f:
        json.dump(answers, f, indent=2)

    n = len(questions)
    avg = total_latency / n if n else 0
    print(f"\n{'='*60}")
    print(f"Results:  {n_ok}/{n} succeeded")
    print(f"Latency:  total={total_latency:.2f}s  avg={avg:.2f}s/question")
    print(f"Output:   {output_path}  (slim)")
    print(f"          {detailed_path}  (with latency)")


if __name__ == "__main__":
    main()
