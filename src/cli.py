"""CLI entry point. Run with: uv run cli (or python -m src.cli)"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()  # load .env before importing agent (which reads env vars at init)

from src.agent import TextToSQLAgent
from src.utils import load_db


def _print_results(results: list[dict]) -> None:
    """Render a list-of-dicts as a plain ASCII table."""
    if not results:
        print("(no rows returned)\n")
        return

    columns = list(results[0].keys())
    # Column widths: max of header length and any cell value length.
    widths = {col: len(col) for col in columns}
    for row in results:
        for col in columns:
            widths[col] = max(widths[col], len(str(row.get(col, ""))))

    sep = "+" + "+".join("-" * (w + 2) for w in widths.values()) + "+"
    header = "|" + "|".join(f" {col:<{widths[col]}} " for col in columns) + "|"

    print(sep)
    print(header)
    print(sep)
    for row in results:
        line = "|" + "|".join(
            f" {str(row.get(col, '')):<{widths[col]}} " for col in columns
        ) + "|"
        print(line)
    print(sep)
    print(f"({len(results)} row{'s' if len(results) != 1 else ''})\n")


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

    model_short = agent.model.split("/")[-1]
    print(f"Text-to-SQL CLI  |  db: {db_path}  |  model: {model_short}")
    print("Type a natural language question, or 'exit' / 'quit' to leave.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not question:
            continue

        if question.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        try:
            sql, results = agent.ask(question)
            print(f"\nSQL:\n{sql}\n")
            _print_results(results)
        except Exception as exc:
            print(f"Error: {exc}\n")


if __name__ == "__main__":
    main()
