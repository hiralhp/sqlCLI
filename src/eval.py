"""Evaluation script — runs questions through the agent and writes answers.

Usage:
    uv run eval                                          # dev questions (default)
    uv run eval -- --questions data/live_questions.json  # live questions
    python -m src.eval --questions data/live_questions.json --output data/live_answers.json

Grading order (when a *_with_answers.json file exists):
  1. Exact text match against gold_answer
  2. Whitespace-normalized text match
  3. Execute gold_sql, compare result rows with float tolerance
  4. LLM judge (fuzzy fallback)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

from src.agent import TextToSQLAgent
from src.utils import load_db, query_db

_DEFAULT_QUESTIONS = Path("data/dev_questions.json")

_JUDGE_PROMPT = """\
Question: {question}
Gold answer: {gold_answer}
Agent's result: {agent_result}

Does the agent's result correctly answer the question?
Respond with PASS or FAIL as your first word, then a colon, then one short reason.
Semantic equivalence, minor float rounding, and row ordering differences count as PASS.
"""


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


def _row_key(row: dict, cols: list[str]) -> tuple:
    """Normalize a result row into a comparable tuple using only the specified columns."""
    vals = []
    for col in cols:
        v = row.get(col)
        if isinstance(v, float):
            vals.append(round(v, 2))
        elif isinstance(v, int):
            vals.append(round(float(v), 2))
        else:
            vals.append(str(v).strip() if v is not None else "")
    return tuple(vals)


def _results_match(agent_rows: list[dict], gold_rows: list[dict]) -> bool:
    """Order-insensitive result set comparison with float tolerance.

    Compares only columns present in the gold result — the agent may return
    extra columns (e.g. PlaylistId alongside Name) without failing the check.
    """
    if len(agent_rows) != len(gold_rows):
        return False
    if not gold_rows:
        return True
    # Use gold column names as the comparison key.
    gold_cols = list(gold_rows[0].keys())
    # Require the agent rows to have at least those columns.
    if not all(col in agent_rows[0] for col in gold_cols):
        return False
    return (
        sorted(_row_key(r, gold_cols) for r in agent_rows)
        == sorted(_row_key(r, gold_cols) for r in gold_rows)
    )


def _judge(client: OpenAI, model: str, question: str, gold_answer: str, agent_result: str) -> tuple[str, str]:
    """LLM fuzzy judge. Returns (verdict, reason) where verdict is 'PASS' or 'FAIL'."""
    prompt = _JUDGE_PROMPT.format(
        question=question,
        gold_answer=gold_answer,
        agent_result=agent_result,
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are an evaluation judge. Follow the user's instructions exactly."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=512,
    )
    msg = response.choices[0].message
    raw = (msg.content or "").strip()
    if not raw:
        extra = getattr(msg, "model_extra", {}) or {}
        raw = (extra.get("reasoning_content") or "").strip()
    upper = raw.upper()
    pass_idx = upper.find("PASS")
    fail_idx = upper.find("FAIL")
    if pass_idx != -1 and (fail_idx == -1 or pass_idx < fail_idx):
        return "PASS", raw[pass_idx + 4:].lstrip(": ").strip() or "Correct."
    elif fail_idx != -1:
        return "FAIL", raw[fail_idx + 4:].lstrip(": ").strip() or "Incorrect."
    return "FAIL", f"Unparseable judge response: {raw!r}"


def _normalize(s: str) -> str:
    return " ".join(s.split())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run eval questions through the SQL agent.")
    parser.add_argument("--questions", type=Path, default=_DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--ids", nargs="+", default=None, help="Only run these question IDs")
    args = parser.parse_args()

    questions_path: Path = args.questions
    output_path: Path = args.output or questions_path.with_name(
        questions_path.stem.replace("_questions", "_answers") + ".json"
    )

    # Auto-detect gold answers file: live_questions.json -> live_questions_with_answers.json
    gold_path = questions_path.with_stem(questions_path.stem + "_with_answers")
    gold_by_id: dict[str, dict] = {}
    if gold_path.exists():
        with open(gold_path) as f:
            for item in json.load(f):
                gold_by_id[item["id"]] = item
        print(f"Gold answers: {gold_path} ({len(gold_by_id)} questions)")
    else:
        print("Gold answers: none found — skipping grading")

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

    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]

    answers: dict[str, dict] = {}
    total_latency = 0.0
    n_ok = 0
    n_pass = 0
    n_judged = 0

    print(f"Questions: {questions_path}")
    print(f"Output:    {output_path}")
    print(f"Running {len(questions)} questions against {db_path}\n")

    for q in questions:
        qid: str = q["id"]
        question: str = q["question"]
        tier: int = q.get("tier", 0)

        print(f"[{qid}] (tier {tier}) {question}")

        agent_rows: list[dict] = []
        t0 = time.perf_counter()
        try:
            sql, agent_rows = agent.ask(question)
            latency = time.perf_counter() - t0
            answer = _summarize(agent_rows)
            answers[qid] = {"sql": sql, "answer": answer, "latency_s": round(latency, 3)}
            n_ok += 1
            preview = answer[:90] + ("..." if len(answer) > 90 else "")
            print(f"  OK  {latency:.2f}s  {preview}")
        except Exception as exc:
            latency = time.perf_counter() - t0
            answers[qid] = {"sql": "", "answer": f"ERROR: {exc}", "latency_s": round(latency, 3)}
            print(f"  FAIL {latency:.2f}s  {exc}")

        # Grading — only if gold data is available.
        if qid in gold_by_id:
            gold = gold_by_id[qid]
            gold_answer = gold.get("gold_answer", "")
            agent_answer = answers[qid]["answer"]
            verdict = reason = None

            # Step 1: exact text match.
            if agent_answer.strip() == gold_answer.strip():
                verdict, reason = "PASS", "Exact match."

            # Step 2: whitespace-normalized text match.
            elif _normalize(agent_answer) == _normalize(gold_answer):
                verdict, reason = "PASS", "Whitespace-normalized match."

            # Step 3: execute gold SQL and compare result sets.
            elif gold_sql := gold.get("gold_sql"):
                try:
                    gold_rows = query_db(conn, gold_sql, return_as_df=False)
                    if _results_match(agent_rows, gold_rows):
                        verdict, reason = "PASS", "Result set matches gold SQL output."
                    else:
                        verdict, reason = None, None  # fall through to LLM
                except sqlite3.Error:
                    verdict, reason = None, None  # gold SQL failed, fall through

            # Step 4: LLM fuzzy judge.
            if verdict is None:
                try:
                    verdict, reason = _judge(
                        agent.client, agent.model,
                        question=question,
                        gold_answer=gold_answer,
                        agent_result=agent_answer,
                    )
                except Exception as exc:
                    verdict, reason = "FAIL", f"Judge error: {exc}"

            answers[qid]["judge_verdict"] = verdict
            answers[qid]["judge_reason"] = reason
            n_judged += 1
            if verdict == "PASS":
                n_pass += 1
            print(f"  {verdict}: {reason}")

        total_latency += latency
        agent.reset_history()
        time.sleep(1)

    # Write results.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        slim = {qid: {"sql": v["sql"], "answer": v["answer"]} for qid, v in answers.items()}
        json.dump(slim, f, indent=2)

    detailed_path = output_path.with_stem(output_path.stem + "_detailed")
    with open(detailed_path, "w") as f:
        json.dump(answers, f, indent=2)

    n = len(questions)
    avg = total_latency / n if n else 0
    print(f"\n{'='*60}")
    print(f"Execution: {n_ok}/{n} succeeded")
    if n_judged:
        print(f"Accuracy:  {n_pass}/{n_judged} PASS ({100*n_pass//n_judged}%)")
    print(f"Latency:   total={total_latency:.2f}s  avg={avg:.2f}s/question")
    print(f"Output:    {output_path}  (slim)")
    print(f"           {detailed_path}  (with latency + verdicts)")


if __name__ == "__main__":
    main()
