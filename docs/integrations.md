# External services and access checks

## Verification on 2026-11-24

| Service | Observed in the implementation environment | Deployment check |
| --- | --- | --- |
| GitHub | The connected repository integration read the brief and wrote the implementation branch. Local integration tests exercise the runtime's REST and Projects v2 GraphQL calls with fixtures. | Use a dedicated deployment credential against the configured management and product repositories. Create a fixture issue through the approval workflow and verify status, Project membership and dates. |
| Discord | An unauthenticated GET to `https://discord.com/api/v10/gateway` returned HTTP 403. The origin of that denial is undetermined. Tests register the command tree with discord.py and invoke the callbacks with synthetic interactions. | Install the bot in a test guild, start it, and check slash-command registration, channel restrictions, a task button and a notification. |
| Meta Model API | An unauthenticated GET to `https://api.meta.ai/v1/models` returned HTTP 401. Meta documents Muse Spark 1.3. | With a project key, check model access and submit a synthetic transcript before using real meeting data. |
| OpenAI API | An unauthenticated GET to `https://api.openai.com/v1/models` returned HTTP 401. OpenAI documents Codex models on the Responses API. | Confirm the selected model for your API project and validate a synthetic revised specification. |

The HTTP probes used no account credentials. Authenticated model inference, Discord delivery and a production Project mutation require deployment-specific verification.

## GitHub

Use a private management repository for the approved specifications and task ledger. Configure the exact product repositories that may receive issues. Give the deployment identity Contents write on management, Issues write on the selected product repositories, metadata read, and Actions read on product repositories to report failed CI. Prefer credentials restricted to the intended resource owner and repositories. Branch rules must permit the service to update its ledger in the management repository. Runtime credentials and PR CI credentials are separate. [Repository contents](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents) · [Issues](https://docs.github.com/en/rest/issues/issues#create-an-issue) · [Workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)

For a GitHub Project, create the board and roadmap views and copy the Project v2 node ID into `github.project_id`. Give the deployment identity write access to that Project. Configure a single-select `Status` with Todo, In Progress, In Review, Blocked, Done and Cancelled. Add date fields named `Start date` and `Target date` to display forecast dates on the roadmap. Projects fields have day-level precision. The Discord Gantt provides the intraday view. With an empty project ID the service operates through issues, the ledger and Discord. A Project synchronization failure queues a notice and leaves the issue available. [GitHub Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)

## Discord

Create an application and bot in the Developer Portal, install it in the configured guild and supply its bot token through `DISCORD_TOKEN`. Use the `bot` and `applications.commands` scopes. Set the guild, member and channel IDs in the TOML configuration. Grant the bot View Channel, Send Messages, Attach Files and Read Message History in the selected channels. Team members need Use Application Commands. Set privacy overrides on the meetings channel before importing any transcripts. After running the service, check `/task list` and use the message context menu on a synthetic transcript. During a Discord outage, mapped GitHub users can queue authorized issue-comment commands for the poller. [Discord application commands](https://docs.discord.com/developers/interactions/application-commands) · [Discord operator guide](discord.md)

## AI and manual operation

- **Muse Spark.** Meta publishes a developer API at `https://api.meta.ai/v1`, including the model ID `muse-spark-1.3`. Configure `AI_PROVIDER=muse`, `MODEL_API_KEY` and `MUSE_MODEL`. Obtain a project key and verify model entitlement in the deployment account. The adapter uses the Responses endpoint with a JSON schema for structured text. [Meta quickstart](https://dev.meta.ai/docs/quickstart) · [Muse Spark model](https://dev.meta.ai/docs/muse-spark)
- **Codex.** OpenAI’s coding guide gives `gpt-6.2-astra` as an example for `POST https://api.openai.com/v1/responses`. Its earlier `gpt-5.3-codex` model is marked deprecated. Configure `OPENAI_API_KEY` and `CODEX_MODEL` for technical consistency checks. The OpenAI provider can also handle the natural-language stages when selected with `AI_PROVIDER=openai`. Check the model ID against your project's enabled models and organization policies. [OpenAI coding guide](https://developers.openai.com/api/docs/guides/code-generation) · [GPT-5.3-Codex status](https://developers.openai.com/api/docs/models/gpt-5.3-codex)
- **Manual.** `AI_PROVIDER=manual` uses operator-supplied Markdown, minutes, a revision-bound AI consistency report and task JSON. An approver attests the report on import. An authorized local AI reviewer can supply the report when external processing consent is absent. If no AI reviewer is available, the team can import, draft, revise and manage existing tasks while approval waits for the required check. [Workflow formats and approval procedure](discord.md)

With the environment loaded, run `python -m relay.ai --probe` to check configured model IDs against provider catalogs. This read-only check requires no meeting text. Test a synthetic request separately to check inference and structured output. [Meta model list](https://dev.meta.ai/docs/api-reference/models/list-models) · [OpenAI model list](https://developers.openai.com/api/reference/resources/models/methods/list)

Obtain participant consent before sending a transcript, revised text or decisions to an external provider. Only use model outputs as proposed content. Discord authorization, GitHub writes, schedules and computed progress run in application code.
