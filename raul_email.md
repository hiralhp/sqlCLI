Hi Raul,

Thanks for the opportunity to work through this.

To evaluate whether an open-source model could replace GPT-5.4 for this workload, I built an interactive Text-to-SQL CLI using the Fireworks inference platform. The CLI translates natural language into SQL, executes it against a SQLite database, displays both the generated SQL and results, and supports follow-up questions through conversational context.

The implementation uses `qwen3-235b-a22b-instruct-2507` by default through Fireworks' OpenAI-compatible API. Since it's a mixture-of-experts model, only a subset of its parameters are active for each token, providing a strong balance of SQL generation quality, latency, and inference cost for an interactive workload.

Beyond the provided baseline, I focused on making the implementation more reliable and production-ready while keeping it simple. I grounded the model with the full database schema and foreign key relationships, restricted generation to read-only queries, added an automatic SQL repair loop that retries using SQLite error messages when execution fails, and rejected unsafe queries before they reach the database.

For validation, I ran the agent against all 10 provided development questions and compared the results against the supplied expected answers. The implementation matched the expected results across all three evaluation tiers. Most queries completed within the requested sub-3-second latency target, with one slower request caused by a transient API rate limit that recovered automatically through exponential backoff and retry.

I also built a regression test suite covering additional edge cases, including prompt injection, destructive SQL requests, empty-result queries, multi-turn conversations, window-function and ranking queries, and SQL repair scenarios. Building those tests uncovered two issues before submission. The safety guard was incorrectly rejecting valid read-only CTEs because it only checked the first SQL token, so I replaced that logic with a sqlglot-based parser that correctly identifies statement types regardless of syntax. I also fixed an edge case where the agent could crash if the model returned an empty response by adding graceful error handling.

From a customer perspective, I believe an open-source model is a strong fit for this use case because text-to-SQL is a narrow, structured task where reliability, latency, and operating cost matter more than broad general-purpose reasoning. While I didn't perform a formal cost benchmark, models like Qwen3 can be served much more economically than large proprietary frontier models, making them a practical option for workloads on the order of 30,000 queries per day while still delivering strong SQL generation quality.

If I were continuing this toward production, my next priorities would be:

* Replace full-schema prompting with schema retrieval for larger customer databases.
* Enforce read-only access through both SQL validation and database permissions.
* Expand the regression suite with execution-based validation across larger, customer-specific schemas.
* Add streaming responses and cache schema metadata to further reduce latency.

I used AI coding assistance to accelerate development, but I personally reviewed and refined the implementation, validated the final system end-to-end, and iterated on the prompt design, schema grounding, retry logic, safety mechanisms, and testing based on the observed results.

I appreciate the opportunity and would be happy to discuss the implementation, design decisions, or how I'd evolve this into a production-ready solution.

Best,
Hiral Patel
