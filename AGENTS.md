# SprintRelay development

Read [the v2 specification](sprint-relay-v2-specification.md) before changing workflow semantics. It supersedes the original brief. The operational agents use the documented MCP contracts. Only a development checkout is used for changes to SprintRelay itself.

- Install `python -m pip install -e '.[test]'`, then run `python -m relay check --config config.example.toml`, `python -m relay migrate --dry-run --config config.example.toml` and `python -m pytest -q`.
- Keep one Core and one private state directory. GitHub continues to own published task/spec state. Preserve stable issue bindings, human edits, approval receipts, baselines, deadlines and actuals across migration and retries.
- Exercise MCP tools through the authorized role and project. Verify consent, expiry, job and fence at the mutation boundary. Keep the same idempotency key across providers and recovery. A successful CLI exit requires an independently validated MCP result.
- Run scheduling, counts, actor checks and approvals in application code. Keep agent job credentials scoped and ephemeral. Operational workers never inherit Core credentials or get a product checkout, shell tools or code-writing capability.
- Keep provider integration within official account-authenticated clients. Record exact live smoke results separately from synthetic tests. Preserve the default disabled/unverified policy for optional clients. Use only fictional transcripts, diffs, CI output and issue text in tests.
- Submit changes and the test results for review in a pull request. Maintain the operating instructions when changing a permission boundary or configuration field.
