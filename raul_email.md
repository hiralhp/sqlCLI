Hi Raul,

Thanks for the opportunity to work through this.

I built an interactive Text-to-SQL CLI using Fireworks-hosted open-source models. The CLI accepts natural language questions, generates SQLite SQL, executes the query against the database, displays both the generated SQL and results, and supports follow-up questions by maintaining a rolling conversation history.

To improve on the baseline prompt, I grounded the model with the full database schema including foreign key relationships, constrained generation to read-only SELECT queries, and added an automatic SQL repair loop that feeds the SQLite error back to the model and retries when execution fails. I included protections such as rejecting non-SELECT statements before they reach the database.

For validation, I ran the agent against all 10 provided development questions and compared the outputs against the supplied gold answers. The implementation produced correct results on all 10 questions across the three evaluation tiers, based on comparison with the provided gold answers. Most queries completed in under 3 seconds, with one slower run caused by a transient API rate limit that was automatically recovered through exponential backoff and retry.

If I were continuing this toward production, my next priorities would be:

- Replace full-schema prompting with schema retrieval for larger customer databases.
- Add stronger SQL validation using an SQL parser and read-only database permissions.
- Expand the evaluation suite with execution-based regression tests and larger, customer-specific schemas.
- Add streaming responses to reduce perceived latency, and cache schema representations to avoid re-building them on each agent restart.

I used AI coding assistance to accelerate implementation, but validated the system against the provided evaluation set and iterated on the prompt, schema grounding, and retry logic based on the results.

I appreciate the opportunity and look forward to discussing the implementation and potential production improvements.

Best,
Hiral Patel
