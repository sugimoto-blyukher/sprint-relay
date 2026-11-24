# Working on SprintRelay

Read sprint-relay-implementation.md before changing workflow semantics. Keep one Linux service and GitHub as the canonical task/spec store. Put member, repository, period and channel choices in config.

- Run `python -m pip install -e '.[test]'`, `python -m relay check --config config.example.toml` and `python -m pytest -q`. Mock external transports and use fictional fixtures.
- Keep deterministic work in relay/core.py and relay/schedule.py. Authorization, task transitions and calendar decisions must remain independent of model output.
- Preserve human edits, the exact reviewed revision, approval receipts, issue bindings, baselines and actuals on retries and scope changes. Validate the complete DAG before publishing.
- Use the configured private location for meeting data. Keep secrets, transcripts, the service database and production configuration out of commits and logs.
- Keep the Discord and GitHub paths on the same state transition code. Review new external permissions and document setup/fallbacks in docs/integrations.md.
- Submit reviewable changes via a pull request. Test failed and retried operations whenever changing publication or synchronization.
