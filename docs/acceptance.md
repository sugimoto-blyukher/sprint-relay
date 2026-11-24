# v2 acceptance

Run `python -m pytest -q` from the repository root with the test extra installed. The hosted Python matrix runs the same suite without deployment credentials. The dedicated Linux containment workflow requires a successful native sandbox test. On a kernel that denies user namespaces, the ordinary suite reports that specific test as skipped.

| ID | Executable coverage / observation |
| --- | --- |
| A01 | `test_ai.py`, `test_e2e.py`: legacy key canaries, no direct inference client, network-disabled meeting-to-issue flow through MCP handlers. |
| A02 | `test_ai.py`, `test_discord_io.py`, `test_e2e.py`: explicit import, named processors, duplicate input, extracted decisions and supporting quotes. |
| A03 | `test_workflow.py`, `test_ai.py`, `test_mcp_server.py`: human review, stale digest, mechanical edits, material changes, blocked approval and privilege boundaries. |
| A04 | `test_core.py`, `test_workflow.py`, `test_ai.py`: fixed assignment, bounded estimates, graph validation, idempotent publication and recovery. |
| A05 | `test_github.py`, `test_bot.py`, `test_e2e.py`: claim/review/accept via Discord and GitHub, Projects synchronization and fallback. |
| A06 | `test_schedule.py`, `test_e2e.py`: CPM, actual availability, period changes, blocked work, immutable baselines/deadlines/actuals and PNG output. |
| A07 | `test_jobs.py`, `test_patrol.py`: deterministic slots, exclusivity, page checkpoints, overdue/blockers without new events. |
| A08 | `test_patrol.py`, `test_jobs.py`, `test_worker.py`: asynchronous audit receipt, bounded SHA context, validated result, stale-head rejection, process exit without submission. |
| A09 | `test_jobs.py`, `test_worker.py`: management/audit candidates, provider-wide limits, permission failures, retries and consent. |
| A10 | `test_jobs.py`, `test_mcp_server.py`: synthetic recovery, late Muse contact, fallback lease protection and safe boundary handover. |
| A11 | `test_jobs.py`, `test_patrol.py`, `test_github.py`: expired writers, fenced results, interruption after remote mutation and deduplication. |
| A12 | `test_mcp_server.py`, `test_github.py`: identity, audience, project and job boundaries, role tool sets and GitHub route restrictions. |
| A13 | `test_worker.py`, `containment.yml`: sanitized environment, dedicated identities, fixed argv, timeout, no host checkout or shell, real read-only kernel mount. |
| A14 | `test_mcp_server.py`, `test_ai.py`, `test_patrol.py`: synthetic injected instructions, consent revocation, controlled public content and evidence-bound actions. |
| A15 | `test_jobs.py`, `test_e2e.py`: all providers offline, human task operations, schedule rendering/sync and recovery of pending work. |
| A16 | `test_cli.py`, `test_workflow.py`: actual SQLite dry-run, upgrade/re-run, legacy approval and history, verified backup and restore. |
| A17 | Live deployment exercise. The authorized development GitHub connection and Codex CLI help were checked. The operator confirmed both synthetic Codex probes in production. Live regular Codex jobs and scheduled Muse interoperability still require acceptance. |

## A17 synthetic deployment exercise

1. Use a private test management repository, a separate fixture product repository, and a test Discord guild. Record the CLI version, contract/account eligibility, date and operator. Use a fictional decision such as “show a preview before export”. Leave actual meeting data out of this trial.
2. Log in using each applicable official client under its isolated OS account. Check required flags and restrictive tool policy. Follow the documented synthetic probe and validate a completed MCP result. A version string, an HTTP response or CLI stdout is insufficient. [Agent setup](agents/)
3. Install the Muse connector using the configured HTTPS resource and exact OAuth identity. Observe two actual scheduled callbacks 45 minutes apart; record their timestamps. Simulate one missed slot and verify that the fallback cannot steal an active lease. Restore access and verify that the next safe slot uses the preferred provider.
4. Run the meeting workflow with that fixture, review the final digest, approve it, and inspect the linked Issue and Project item. Use a Discord-only designer to claim work and submit an artifact. Verify the notification and a PR review tied to a specific head SHA on the fixture repository. [Discord guide](discord.md)
5. Remove test access and verify that the service continues human task management and forecasting. Keep a sanitized record of passes, denials, timeouts and outstanding account-specific limitations. Enable only the clients that passed under the target deployment's policy.


The Codex project-job regression runs a patrol, its PR audit, and an imported meeting through draft, human review, validation, approval and planning. The native test uses the official 0.172.0 executable, scripted loopback inference and a synthetic GitHub transport. Set `SPRINTRELAY_TEST_CODEX` to run it locally. [Native regression](../tests/test_codex_jobs.py) · [Live acceptance](deployment.md#codex-通常ジョブの更新と受け入れ)
