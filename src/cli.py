"""CLI entry point. Run with: uv run cli (or python -m src.cli)"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()  # load .env before importing agent (which reads env vars at init)

from src.agent import TextToSQLAgent
from src.utils import load_db, print_table_schema

_HELP_TEXT = """
Commands:
  /help          Show this message
  /schema        Show the full database schema
  /schema <table> Show schema for a specific table
  /reset         Clear conversation history and start a fresh session
  exit           Quit the CLI
  quit    Quit the CLI

Example questions:
  What are the top 5 best-selling genres?
  Which artist has sold the most tracks?
  Show total revenue by country, ordered from highest to lowest.

Tip: follow-up questions are supported — the agent remembers the last 5 exchanges.
"""


def _print_results(results: list[dict]) -> None:
    """Render a list-of-dicts as a plain ASCII table."""
    if not results:
        print("Query ran successfully but returned no rows.\n")
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
    print("Ask a question in plain English. Type /help for examples, or exit to quit.")
    print("Follow-up questions are supported — the agent remembers recent context.\n")

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

        if question.lower() == "/help":
            print(_HELP_TEXT)
            continue

        if question.lower() == "/reset":
            agent.reset_history()
            print("Context cleared. Starting a fresh session.\n")
            continue

        if question.lower() == "/schema" or question.lower().startswith("/schema "):
            parts = question.split(maxsplit=1)
            table = parts[1] if len(parts) > 1 else None
            print_table_schema(conn, table)
            continue

        try:
            sql, results = agent.ask(question)
            print(f"\nSQL:\n{sql}\n")
            _print_results(results)
        except Exception as exc:
            print(f"Something went wrong: {exc}\n")


if __name__ == "__main__":
    main()
