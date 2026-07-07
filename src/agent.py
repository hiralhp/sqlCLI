"""Agent logic for text-to-SQL conversion."""

import os
import re
import sqlite3
import time
from typing import Optional

import sqlglot
from sqlglot import exp as sql_exp

from openai import OpenAI, RateLimitError

from src.utils import get_schema, query_db

DEFAULT_MODEL = "accounts/fireworks/models/qwen3-235b-a22b-instruct-2507"

# System prompt injected at the start of every conversation.
# The schema placeholder is filled in at init time.
_SYSTEM_TEMPLATE = """\
You are a SQLite SQL expert for the Chinook digital music store database.
Convert natural language questions into valid SQLite SELECT queries.

RULES:
- Output ONLY the raw SQL query — no markdown, no code fences, no explanation.
- Only generate SELECT statements. Never use INSERT, UPDATE, DELETE, DROP, CREATE, or any DDL/DML.
- Use only the exact table and column names listed in the schema below. Never invent names.
- Use standard SQLite syntax (e.g. strftime('%Y', date_col) for year extraction).
- Always terminate the query with a semicolon.

DATABASE SCHEMA:
{schema}
"""


class TextToSQLAgent:
    """Converts natural language questions to SQLite SQL using a Fireworks-hosted LLM.

    Maintains a rolling conversation history so follow-up questions can
    reference previous context.  Each call to ``ask`` appends the
    user/assistant exchange to ``self.history``.

    Args:
        conn: Open SQLite connection to the target database.
        model: Fireworks model ID. Defaults to the FIREWORKS_MODEL env var,
               or ``DEFAULT_MODEL`` if that is not set.
    """

    def __init__(self, conn: sqlite3.Connection, model: Optional[str] = None) -> None:
        api_key = os.environ.get("FIREWORKS_API_KEY")
        if not api_key:
            raise ValueError(
                "FIREWORKS_API_KEY environment variable is not set. "
                "Create a .env file or export the variable before running."
            )

        self.model = model or os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL)
        self.conn = conn
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.fireworks.ai/inference/v1",
        )
        self._system_prompt = _SYSTEM_TEMPLATE.format(schema=self._build_schema_text())
        # Rolling message history — user/assistant turns only (not system).
        self.history: list[dict] = []

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def _build_schema_text(self) -> str:
        """Return a CREATE TABLE-style schema string with FK annotations."""
        schema = get_schema(self.conn)
        blocks: list[str] = []

        for table_name, columns in schema.items():
            # Fetch foreign-key metadata for this table.
            fk_cursor = self.conn.execute(f"PRAGMA foreign_key_list({table_name})")
            fk_rows = fk_cursor.fetchall()
            # Map: local_col -> (referenced_table, referenced_col)
            fk_map: dict[str, tuple[str, str]] = {}
            for row in fk_rows:
                row_dict = dict(row)
                fk_map[row_dict["from"]] = (row_dict["table"], row_dict["to"])

            col_defs: list[str] = []
            for col in columns:
                parts = [col["name"], col["type"]]
                if col["pk"]:
                    parts.append("PRIMARY KEY")
                elif col["notnull"]:
                    parts.append("NOT NULL")
                if col["name"] in fk_map:
                    ref_table, ref_col = fk_map[col["name"]]
                    parts.append(f"REFERENCES {ref_table}({ref_col})")
                col_defs.append("  " + " ".join(parts))

            block = f"CREATE TABLE {table_name} (\n" + ",\n".join(col_defs) + "\n);"
            blocks.append(block)

        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # LLM interaction
    # ------------------------------------------------------------------

    def _call_llm(self, messages: list[dict], _retries: int = 3) -> str:
        """Call the LLM, retrying with exponential backoff on rate-limit errors."""
        delay = 10  # seconds before first retry
        for attempt in range(_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0,
                    max_tokens=512,
                )
                content = response.choices[0].message.content
                if not content or not content.strip():
                    raise ValueError(
                        "The model could not generate valid SQL. "
                        "Please try rephrasing your question."
                    )
                return content.strip()
            except RateLimitError:
                if attempt >= _retries:
                    raise
                print(f"  [rate limit] waiting {delay}s before retry...")
                time.sleep(delay)
                delay *= 2

    def _clean_sql(self, raw: str) -> str:
        """Strip markdown code fences and extraneous whitespace."""
        raw = re.sub(r"```(?:sql)?\s*", "", raw, flags=re.IGNORECASE)
        raw = raw.replace("```", "").strip()
        return raw

    def _is_safe_query(self, sql: str) -> bool:
        # sqlglot is used instead of a hand-rolled tokenizer because correctly
        # handling quoted identifiers, block comments, string literals, and nested
        # parentheses is non-trivial. A naive first-token check would reject valid
        # CTEs (WITH ... SELECT) and could be fooled by edge-case syntax.
        #
        # In sqlglot, both plain SELECT and WITH...SELECT (CTEs) parse to
        # exp.Select — the CTE becomes an attribute of the Select node — so a
        # single isinstance check covers both cases. Anything else (INSERT,
        # UPDATE, DELETE, DROP, CREATE, PRAGMA, …) parses to a different node
        # type and is rejected.
        #
        # If sqlglot cannot parse the input at all, we reject it to stay safe.
        try:
            stmt = sqlglot.parse_one(sql, dialect="sqlite")
        except Exception:
            return False
        return isinstance(stmt, sql_exp.Select)

    def _build_messages(self, user_text: str) -> list[dict]:
        system_msg = {"role": "system", "content": self._system_prompt}
        return [system_msg] + self.history + [{"role": "user", "content": user_text}]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ask(self, question: str, max_retries: int = 2) -> tuple[str, list[dict]]:
        """Convert *question* to SQL, execute it, and return ``(sql, rows)``.

        On SQL execution failure the agent asks the model to repair the query
        using the error message, retrying up to *max_retries* additional times.

        Raises:
            ValueError: If the generated SQL is not a SELECT statement.
            sqlite3.Error: If the query fails after all retry attempts.
        """
        messages = self._build_messages(question)
        raw = self._call_llm(messages)
        sql = self._clean_sql(raw)

        if not self._is_safe_query(sql):
            raise ValueError(
                f"Model returned a non-SELECT statement — refusing to execute.\nSQL: {sql}"
            )

        last_error: Optional[str] = None
        for attempt in range(max_retries + 1):
            try:
                results = query_db(self.conn, sql, return_as_df=False)
                # Persist this exchange in history for follow-up questions.
                self.history.append({"role": "user", "content": question})
                self.history.append({"role": "assistant", "content": sql})
                # Keep the window bounded to the last 5 exchanges (10 messages).
                if len(self.history) > 10:
                    self.history = self.history[-10:]
                return sql, results

            except sqlite3.Error as exc:
                last_error = str(exc)
                if attempt >= max_retries:
                    break

                # Ask the model to repair the broken SQL.
                repair_prompt = (
                    f"The SQL query below failed with this SQLite error:\n"
                    f"  {last_error}\n\n"
                    f"Broken SQL:\n{sql}\n\n"
                    "Output only the corrected SQL query."
                )
                repair_messages = messages + [
                    {"role": "assistant", "content": sql},
                    {"role": "user", "content": repair_prompt},
                ]
                raw = self._call_llm(repair_messages)
                sql = self._clean_sql(raw)

                if not self._is_safe_query(sql):
                    raise ValueError(
                        f"Repaired query is not a SELECT statement.\nSQL: {sql}"
                    )

        raise sqlite3.Error(
            f"Query failed after {max_retries + 1} attempt(s). "
            f"Last error: {last_error}\nFinal SQL: {sql}"
        )

    def reset_history(self) -> None:
        """Clear the conversation history (e.g. between independent eval questions)."""
        self.history = []
