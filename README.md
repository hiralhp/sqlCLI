# Fireworks AI Text-to-SQL CLI

An interactive CLI agent that converts natural language questions into SQLite SQL
and executes them against the Chinook database, using the Fireworks AI API via
the OpenAI-compatible Python SDK.

---

## Features

- Interactive CLI with conversational follow-up questions
- Automatic SQL repair using SQLite error feedback
- Read-only SQL enforcement with sqlglot-based statement validation
- Conversation history with bounded rolling context (last 5 exchanges)
- Batch evaluation against the provided 10-question development set
- Exponential backoff for Fireworks API rate limits

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Fireworks AI API key

---

## Setup

```bash
uv sync
```

Create a `.env` file in the project root:

```
FIREWORKS_API_KEY=your_key_here
FIREWORKS_MODEL=accounts/fireworks/models/qwen3-235b-a22b-instruct-2507
```

`FIREWORKS_MODEL` is optional — the value above is the default.

---

## Run

```bash
uv run cli
```

Type a natural language question at the prompt. Use `/help` to see example
questions and available commands. Type `exit` or `quit` to end the session.

---

## Run evaluation

```bash
uv run eval
```

Runs all 10 questions from `data/dev_questions.json` and writes:

- `data/dev_answers.json` — submission format (`sql` + `answer` per question)
- `data/dev_answers_detailed.json` — same, plus `latency_s` per question

To compare against gold-standard SQL and expected results, see
`data/dev_questions_with_answers.json`.

---

## Run regression tests

```bash
uv run regression
```

Runs a suite of edge case tests covering prompt injection, destructive SQL
requests, empty-result queries, multi-turn conversation memory,
window-function and ranking queries, and SQL repair scenarios.

---

## Architecture

```
src/
├── agent.py      # TextToSQLAgent — LLM calls, schema injection, safety guard, retry loop
├── cli.py        # REPL: reads input, calls agent.ask(), prints SQL + ASCII table
├── eval.py       # Batch runner: loops over dev questions, writes JSON output
├── regression.py # Edge case regression suite
└── utils.py      # DB helpers: load_db, query_db, get_schema
```

`TextToSQLAgent` is the core. It owns the OpenAI client (pointed at Fireworks),
the system prompt, the conversation history, and the retry logic. `cli.py` and
`eval.py` are thin shells that instantiate the agent and feed it input.

---

## Prompt design

The system prompt is built once at `__init__` time and injected at the top of
every API call. It has two parts:

**Hard rules** — sent verbatim on every request:

```
- Output ONLY the raw SQL query — no markdown, no code fences, no explanation.
- Only generate SELECT statements. Never use INSERT, UPDATE, DELETE, DROP, CREATE, or any DDL/DML.
- CTEs (WITH ... SELECT ...) are allowed and preferred for multi-step computations such as ranking over aggregates.
- Use only the exact table and column names listed in the schema below. Never invent names.
- Use standard SQLite syntax (e.g. strftime('%Y', date_col) for year extraction).
- Always terminate the query with a semicolon.
- SQLite does not allow column aliases from the same SELECT to be referenced inside OVER clauses.
  Use a CTE or repeat the full expression (e.g. RANK() OVER (ORDER BY SUM(col) DESC)).
```

**Schema** — every table rendered as a `CREATE TABLE` statement with column
types, `PRIMARY KEY` markers, and `REFERENCES` annotations derived from
`PRAGMA foreign_key_list`. This gives the model an unambiguous view of every
join path without requiring it to guess foreign keys.

```sql
CREATE TABLE Invoice (
  InvoiceId INTEGER PRIMARY KEY,
  CustomerId INTEGER NOT NULL REFERENCES Customer(CustomerId),
  InvoiceDate DATETIME NOT NULL,
  ...
);
```

Temperature is set to `0` and `max_tokens` to `1024` to keep output
deterministic and to accommodate complex multi-CTE queries.

---

## Retry loop

`agent.ask()` has two independent retry mechanisms:

**SQL repair loop** (`max_retries=2`, i.e. up to 3 total attempts)

1. Call the LLM and strip any markdown fences from the response.
2. Validate the parsed statement is read-only using sqlglot — unsafe statements
   (INSERT, UPDATE, DELETE, DROP, etc.) are rejected before touching the database.
3. Execute the query. If it succeeds, return `(sql, rows)`.
4. If `sqlite3.Error` is raised, send the broken SQL and the error message back
   to the model with a targeted repair prompt:

   ```
   The SQL query below failed with this SQLite error:
     <error text>

   Broken SQL:
   <sql>

   Output only the corrected SQL query.
   ```

5. Validate and re-execute the repaired query. Repeat up to `max_retries` times.
   If all attempts fail, raise the last `sqlite3.Error`.

**Rate-limit backoff** (inside `_call_llm`, 3 retries)

If the Fireworks API returns a `RateLimitError`, `_call_llm` waits with
exponential backoff starting at 10 seconds (`10s → 20s → 40s`) before retrying.
This is separate from the SQL repair loop and transparent to the caller.

---

## Conversation history

`self.history` is a list of `{"role": ..., "content": ...}` dicts, one per
user/assistant turn. On every call to `ask()`, `_build_messages()` prepends the
system prompt and appends the current question:

```python
[system_msg] + self.history + [{"role": "user", "content": question}]
```

After a successful query, the exchange is appended to history:

```python
self.history.append({"role": "user",      "content": question})
self.history.append({"role": "assistant", "content": sql})
```

The window is capped at the last 10 messages (5 exchanges) to keep context
size bounded. Failed queries are not appended — history only records successful
turns.

`reset_history()` clears the list. The eval script calls this between questions
so each is answered independently with no context leakage.

---

## Evaluation

`src/eval.py` runs a batch evaluation against `data/dev_questions.json`, which
contains 10 questions across three complexity tiers:

| Tier | Description | Examples |
|---|---|---|
| 1 | Single-table lookups, simple aggregations | Top genres, albums by artist |
| 2 | Multi-table JOINs, GROUP BY, date filtering | Revenue per year, avg invoice by country |
| 3 | Window functions, subqueries, multi-hop joins | Customer rank, artists in multiple genres |

For each question the script:

1. Times the full round-trip with `time.perf_counter()`.
2. Calls `agent.ask(question)` and summarizes up to 5 result rows.
3. Calls `agent.reset_history()` before the next question.
4. Sleeps 1 second between questions to stay within API rate limits.

---

## Results

- All 10 development questions passed across all three tiers.
- Most questions completed in under 3 seconds end-to-end (LLM call + SQL execution).
- The exponential backoff in `_call_llm` recovered from one Fireworks rate-limit
  event automatically, with no failure surfaced to the caller.

---

## Limitations

**SQLite only** — the schema builder uses SQLite-specific pragmas
(`PRAGMA table_info`, `PRAGMA foreign_key_list`) and the system prompt includes
SQLite syntax guidance. Supporting other databases requires changes to both
`utils.py` and the prompt.

**Parser-based read-only validation, not database-level enforcement** — generated
SQL is validated with sqlglot to ensure it is a read-only statement before
execution. However, there is no database-level read-only permission enforced on
the connection, no query cost limit, and no `EXPLAIN` dry-run. Invalid queries
that pass validation are caught when SQLite raises an error, at which point the
repair loop activates.

**Bounded follow-up memory** — conversation history is a flat rolling window of
the last 5 exchanges (10 messages). There is no summarization, entity tracking,
or semantic retrieval. Context older than 5 turns is silently dropped.

**Full-schema prompting** — the entire database schema is injected into every
request. This works well for Chinook but does not scale to large production
schemas with hundreds of tables.

---

## Future improvements

- Retrieve only relevant tables instead of injecting the full schema on every request.
- Enforce read-only access at both the parser and database connection permission layers.
- Add execution-based regression testing against larger, customer-specific schemas.
- Stream responses for improved CLI responsiveness on longer queries.
- Cache schema metadata to avoid rebuilding it on each agent restart.
- Consider extending beyond SQLite to PostgreSQL and MySQL.
