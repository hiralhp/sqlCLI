Hi Raul,



Thanks for the opportunity to work through this.



I built an interactive Text-to-SQL CLI using Fireworks-hosted open-source models that translates natural language into SQL, executes it against a SQLite database, displays the generated SQL and results, and supports follow-up questions through conversational context. Beyond the provided baseline, I focused on making the implementation more reliable and production-ready while keeping it simple.



To improve on the baseline, I grounded the model with the full database schema and foreign key relationships, restricted generation to read-only queries, added an automatic SQL repair loop that retries using SQLite error messages when execution fails, and rejected unsafe queries before they reach the database.



For validation, I ran the agent against all 10 provided development questions and compared the results against the supplied expected answers. The implementation matched the expected results across all three evaluation tiers.



I also built a regression test suite covering additional edge cases, including prompt injection, destructive SQL requests, empty-result queries, multi-turn conversations, window-function and ranking queries, and SQL repair scenarios. Building those tests uncovered two issues before submission. The safety guard was incorrectly rejecting valid read-only CTEs because it only checked the first SQL token, so I replaced that logic with a sqlglot-based parser that correctly identifies statement types regardless of syntax. I also fixed an edge case where the agent could crash if the model returned an empty response by adding graceful error handling.



Throughout the implementation, I tried to optimize for what I think a customer would care about most: a system that fails safely, is easy to understand, and can be extended to larger production databases.



If I were continuing this toward production, my next priorities would be:



* Replace full-schema prompting with schema retrieval for larger customer databases.
* Enforce read-only access through both SQL validation and database permissions.
* Expand the regression suite with execution-based validation across larger, customer-specific schemas.
* Add streaming responses and cache schema metadata to reduce latency.



I used AI coding assistance to accelerate implementation, but I personally reviewed and refined all generated Python code, validated the final system end-to-end, and iterated on the prompt design, schema grounding, retry logic, safety mechanisms, and testing based on the observed results.



I appreciate the opportunity and would be happy to discuss the implementation, design decisions, or how I'd evolve this into a production-ready solution.



Best,



Hiral Patel



