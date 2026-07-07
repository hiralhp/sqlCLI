# Fireworks AI Text-to-SQL CLI

An interactive CLI agent that converts natural language questions into SQLite SQL
and executes them against the Chinook database, using the Fireworks AI API via
the OpenAI-compatible Python SDK.

---

## Setup

**Install dependencies**

```bash
uv sync
```

**Create `.env`**

```
FIREWORKS_API_KEY=...
FIREWORKS_MODEL=...
```

`FIREWORKS_MODEL` is optional — defaults to `accounts/fireworks/models/qwen3-235b-a22b-instruct-2507`.

---

## Run

```bash
uv run cli
```

Type a natural language question at the prompt. Type `exit` or `quit` to end the session.

---

## Run evaluation

```bash
uv run eval
```

Runs all 10 questions from `data/dev_questions.json` and writes:

- `data/dev_answers.json` — submission format (`sql` + `answer` per question)
- `data/dev_answers_detailed.json` — same, plus `latency_s` per question

---

## Architecture

```
src/
├── agent.py   # TextToSQLAgent — LLM calls, schema injection, retry loop
├── cli.py     # REPL: reads input, calls agent.ask(), prints SQL + ASCII table
├── eval.py    # Batch runner: loops over dev questions, writes JSON output
└── utils.py   # DB helpers: load_db, query_db, get_schema
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
- Use only the exact table and column names listed in the schema below. Never invent names.
- Use standard SQLite syntax (e.g. strftime('%Y', date_col) for year extraction).
- Always terminate the query with a semicolon.
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

Temperature is set to `0` and `max_tokens` to `512` to keep output deterministic
and constrained to SQL only.

---

## Retry loop

`agent.ask()` has two independent retry mechanisms:

**SQL repair loop** (`max_retries=2`, i.e. up to 3 total attempts)

1. Call the LLM, clean markdown fences from the response, check the first token
   is `SELECT` — reject anything else before touching the database.
2. Execute the query. If it succeeds, return `(sql, rows)`.
3. If `sqlite3.Error` is raised, send the broken SQL and the error message back
   to the model with a targeted repair prompt:

   ```
   The SQL query below failed with this SQLite error:
     <error text>

   Broken SQL:
   <sql>

   Output only the corrected SQL query.
   ```

4. Clean and validate the repaired query, then retry execution. Repeat up to
   `max_retries` times. If all attempts fail, raise the last `sqlite3.Error`.

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

The window is capped at the last 10 messages (5 exchanges) to keep the context
size bounded. Failed queries are not appended — the history only records
successful turns.

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

Output is written to two files:

- **`dev_answers.json`** — `{ "<id>": { "sql": "...", "answer": "..." }, ... }` —
  matches the submission format in `dev_answers_example.json`.
- **`dev_answers_detailed.json`** — same structure plus `"latency_s"` per entry.

To compare against gold-standard SQL and expected results, see
`data/dev_questions_with_answers.json`.

---

## Results

- **10/10** dev questions answered correctly across all three tiers.
- Most queries complete in **under 3 seconds** end-to-end (LLM call + execution).
- One question triggered Fireworks rate limiting mid-run; the exponential backoff
  in `_call_llm` recovered automatically with no failure surfaced to the caller.

---

## Limitations

**SQLite only** — the schema builder and query executor use SQLite-specific
pragmas (`PRAGMA table_info`, `PRAGMA foreign_key_list`) and SQLite syntax
assumptions baked into the system prompt. Switching databases requires changes
in both `utils.py` and the prompt.

**SELECT only** — every generated query is checked with a first-token guard.
Anything that is not a `SELECT` statement is rejected outright. There is no
deeper parse; a malformed query that starts with `SELECT` but contains embedded
DDL would pass the guard and fail at execution time (triggering the repair loop).

**Simple follow-up memory** — conversation history is a flat rolling window of
the last 5 exchanges (10 messages). There is no summarization, no entity
tracking, and no semantic retrieval. Long sessions or questions that depend on
context older than 5 turns will lose that context silently.

**Simple SQL validation** — the only pre-execution check is the `SELECT` token
guard. There is no syntax validation, no schema-name verification, and no
`EXPLAIN` dry-run before executing. Invalid queries are caught only when
SQLite raises an error, at which point the repair loop activates.
