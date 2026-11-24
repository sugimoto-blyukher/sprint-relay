# Linux / LXC deployment and recovery

## スクリプトを使ったセットアップと再デプロイ

レビュー済みのチェックアウトまたは配布アーカイブを Linux の設置先へ置き、リポジトリ直下で実行します。アプリは `/opt/sprintrelay`、設定は `/etc/sprintrelay`、状態は `/var/lib/sprintrelay` です。前提は systemd、Python 3.11 以上と venv、依存パッケージを取得できる Python パッケージ環境です。Debian 系では `python3-venv` が必要になる場合があります。

```sh
sudo bash deploy/setup.sh
sudo bash deploy/redeploy.sh
```

- **初回セットアップ**は Core の専用ユーザーと非公開の状態ディレクトリを用意し、用意済みの TOML またはテンプレートの編集で設定を取り込みます。既存の設定とトークンファイルは再利用します。新しいトークンは端末で非表示入力し、root 所有の 0600 ファイルに保存します。オプションの Codex Worker には、レビュー済みのネイティブバイナリ、Bubblewrap、ループバック限定の Squid とその許可リスト、専用ユーザーでの公式ログインが必要です。スクリプトは Codex のログインを代行せず、既存のプロキシ設定も書き換えません。 [Worker の前提条件](agents/)
- **再デプロイ**は指定されたチェックアウトをインストールします。新しい仮想環境を準備して設定を検証し、Core と Worker を停止して SQLite のサイドカーを含む状態・設定・ユニットを `/var/backups/sprintrelay` に保存します。移行 dry-run の後、既存の drop-in を残したままベースユニットを更新し、稼働していた Worker ソケットと Core を起動します。ループバックの MCP から未認証 HTTP 401 が返ることを確認します。確認を省く場合は `--yes` を指定します。
- **失敗からの復旧**では旧環境とベースユニットを復元します。新しい Core が起動を試みた後は、データベースの互換性と稼働中のサービスを確認してから旧 Core を起動してください。GitHub への公開やその他の外部副作用を取り消さないため、スクリプトは SQLite を自動復元しません。成功時も旧仮想環境とバックアップを保存します。本番疎通を確認した後、必要な保持期間に従って整理してください。

既存環境で Probe 障害修正を反映する場合は `[jobs]` の `probe_diagnostics = true` と Worker の drop-in を確認し、再デプロイ後に両役割を実アカウントで検証します。 [実環境受け入れ手順](#worker-更新と-codex-の実環境受け入れ)

## Core and credentials

Use one Core per management repository. The GitHub ledger contains published tasks, issue bindings and scheduling history. The private local directory contains SQLite, optional calendar overrides and the service lock. GitHub App installations scoped independently to the management and product repositories are preferred. Supply current installation tokens from your credential manager and refresh them before expiry. For a small deployment, two distinct fine-grained PATs scoped to the appropriate repositories are supported. Automatic GitHub App private-key exchange is outside the service.

Install the package into `/opt/sprintrelay/.venv` from the reviewed checkout. Keep that checkout, the virtual environment, and the unit files administrator-owned. Create an unprivileged system account named `sprintrelay`; provide its group read access to the operational TOML only. Create `/etc/sprintrelay/core.env` as a root-owned 0600 systemd EnvironmentFile containing the three values in `.env.example`. Scope product permissions to the selected repositories. Ordinary PR discussion comments use the Issues API. Project mutations require access to the selected Project. Keep the management repository private and writable by this identity.

Install `deploy/sprintrelay.service` under `/etc/systemd/system`. Put the configured TOML at `/etc/sprintrelay/config.toml`. For example, from the deployment checkout:

```sh
sudo install -m 0644 deploy/sprintrelay.service /etc/systemd/system/sprintrelay.service
sudo systemctl daemon-reload
sudo -u sprintrelay /opt/sprintrelay/.venv/bin/python -m relay check --config /etc/sprintrelay/config.toml
sudo systemctl enable --now sprintrelay.service
```

For AI workers, use the separate gateway socket/service templates and per-vendor login instructions. A missing provider or unavailable isolation remains disabled or degraded. Keep the official user login exclusively with its dedicated worker account. Confirm permissible headless use and the exact CLI tool restrictions with synthetic data before enabling the provider. [Worker provisioning and common prompts](agents/)

## MCP exposure

The local MCP endpoint defaults to `http://127.0.0.1:8371/mcp`. Each local launch receives a fresh job-bound credential, expiry and lease. The Core exposes the same domain services to Discord and MCP. The service starts with remote access disabled.

Remote Muse access additionally requires an OAuth authorization server that supports the connector's flow, a registered client, exact subject, issuer, HTTPS JWKS URL, resource audience and `sprintrelay:management` scope. Set all `mcp` fields in the example configuration. Route **only** the remote loopback listener (default 8372) through an HTTPS reverse proxy. Overwrite the inbound Host and X-Forwarded-Proto headers; never proxy the local worker port. Restrict network access to the public TLS port. The private worker service accounts must have no access to other local services or the Core state directory. Example nginx locations inside a TLS-enabled virtual host for the configured resource:

```nginx
location = /mcp {
    client_max_body_size 5m;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_set_header Host relay.example;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8372;
}
location = /.well-known/oauth-protected-resource/mcp {
    proxy_set_header Host relay.example;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8372;
}
```

Replace the example host with the exact configured resource hostname. Configure the proxy to omit Authorization headers, query strings, request bodies and private results from logs. Validate TLS, OAuth discovery, rejection of a wrong audience/subject and authorized `tools/list` from a synthetic connector. Only enable real Muse scheduling after checking a complete 45-minute patrol, renewal, checkpoint and a deliberately missed slot. If the account cannot perform this flow, keep Muse unavailable.

## Existing installation

Stop the v1 service before upgrading. Retain the original binary/environment and copy the **entire private state directory** (including SQLite sidecars and calendar overrides) to a private backup. Preserve the existing bot IDs, named GitHub repositories and project. The v2 project boundary is the management repository name. A v1 blanket consent does not authorize named external processors in v2.

```sh
python -m relay migrate --config config.toml --state-dir state --dry-run
python -m relay migrate --config config.toml --state-dir state
python -m relay status --config config.toml --state-dir state
python -m relay doctor --config config.toml
```

The dry-run uses an in-memory copy. The first real upgrade creates a verified `pre-v2-*.sqlite3` online SQLite backup (including committed WAL pages), then applies additive tables and legacy workflow backfill in a transaction. Repeating the command preserves the existing version. Known historical actors and digests remain intact; unknown historical timestamps stay unknown. Reuse of the published GitHub issue numbers and the private specification archive requires no GitHub migration. Remove obsolete AI credentials from the service environment at deployment.

To make another database backup, stop the service and run `python -m relay backup --config config.toml --state-dir state --output /private/backup/relay.sqlite3`. The destination directory must be 0700 and the destination filename unused. For rollback, stop all writers, quarantine the current state directory, restore the full backup to a **fresh** 0700 directory with its original owner, then start the matching software version. To restore just the SQLite backup, put it in the fresh directory as `sprint-relay.sqlite3` (0600) and restore the matching calendar overrides separately. Inspect GitHub for activity since that backup before resuming publication. A database rollback cannot undo Issues or comments already posted.

## Operation and trust boundaries

- `/relay status` shows routing, health and patrol state. Only approved operators can pause/resume AI processing. Pending jobs retain their IDs across failover. A provider whose authorization expires requires ordinary account reauthentication. Review synthetic proof and subsequent job receipts in the configured deployment.
- Save TOML changes atomically. Agent enablement, project-data recipients, patrol period, watchdog, job timeout, polling and retention settings are reread at runtime. Identity/endpoint changes require a restart. Update the Muse-side schedule after changing the patrol interval. Use Discord's schedule update command for event period and availability.
- The management agent can request an audit, propose tasks, submit spec artifacts and use the restricted patrol actions. The audit agent reads only the assigned target. Component access checks use the verified identity, project, job, expected revision and current lease. Human acceptance and approval enter through authenticated human operations.
- The product credential stays in Core. The adapter constrains GitHub resource paths and operations; GitHub must independently grant **no product Contents write** to that credential. CLI workers receive no Core credential. The worker sandbox uses a fresh filesystem, fixed native client, separate OS identity and bounded process group. The shipped configuration provides read-only audit evidence through MCP and does not run repository tests. Host administrators, root-managed images, the OAuth issuer and explicitly authorized data processors are within the trust boundary.
- Raw transcripts expire according to `privacy.raw_retention_days`. Scope, protect and expire backups independently. If the SQLite volume is unavailable, stop the service and recover the state before restarting. If GitHub is unavailable, leave operations pending. Reconcile with the existing job/receipt on recovery. Notify operators if deployment access to the Project, Issues or Discord channel is revoked.

### Technical evidence

Product audits receive a bounded excerpt of the pinned commit or PR comparison (up to eight eligible text patches and sixty lines each). A CI audit includes the failed run/attempt metadata and commit changes. Source trees, raw CI logs and hooks stay outside the worker. Core renders a repair Issue or a `COMMENT` review using the category, severity, acceptance condition, evidence link and private finding reference. The review carries the exact audited head in `commit_id`. Source links identify the file at that revision and the first visible hunk when available. A CI evidence link identifies the run attempt. Authorized managers can read the underlying assessment with `get_audit_result`. If the head, comparison base, run attempt or task version changes, the manager must obtain current evidence before acting.

A repair Issue records a finding for triage. To commit team capacity or alter existing scope, include its work in the human-reviewed specification and task plan. Automatic issue creation therefore does not invent an owner, effort estimate or deadline.

Use `python -m relay status --report --config config.toml --state-dir state` in a private operator terminal to read the latest retained patrol summary and its validated references. Discord receives team-visible counts. Treat report text as untrusted data.

For PR and CI audit launches the same filtered evidence is mounted as a disposable, read-only JSON snapshot. The mount carries the assigned job ID and exact commit SHA. A live lease and provider consent are checked before export. The agent does not receive a cloned repository or per-file paths supplied by the model.

## Worker 更新と Codex の実環境受け入れ

Issue #3〜#5 の更新は SQLite のスキーマ変更を伴いません。正式仕様の自動復旧に加え、設定済みの承認者がローカル CLI の合成 Probe を要求できるようになりました。

通常は冒頭の `deploy/redeploy.sh` を使用します。以下はスクリプトを使わず個別に更新する場合の手順です。

1. Core を停止し、既存の仮想環境、ユニットと drop-in、非公開設定、状態ディレクトリをバックアップします。SQLite を個別にバックアップする場合は上記の `relay backup` を使います。確認したソース一式を配布し、既存の Core 用仮想環境にインストールします。Worker と Core は同じリリースの Python パッケージを使用してください。
2. 既存の TOML の `[jobs]` に `probe_diagnostics = true` を追加します。設定チェックを実行し、Worker ユニットの最新版を配置します。既存の drop-in は `systemctl cat` で確認し、今回の暫定設定だけを整理します。他のローカル制約も含む drop-in ファイルは必要な設定を残してください。
3. systemd にユニットを読み直させ、Core 停止中に古い Codex Worker を停止します。既存の socket activation を使って次のジョブで新しい Worker が起動します。

```sh
sudo systemctl stop sprintrelay.service
# レビュー済みのソース一式を /opt/sprintrelay に配布し、バックアップ後に実行
sudo /opt/sprintrelay/.venv/bin/python -m pip install /opt/sprintrelay
sudo -u sprintrelay /opt/sprintrelay/.venv/bin/python -m relay check --config /etc/sprintrelay/config.toml
sudo install -m 0644 /opt/sprintrelay/deploy/sprintrelay-worker@.service /etc/systemd/system/
sudo systemctl cat sprintrelay-worker@codex.service
sudo systemctl daemon-reload
sudo systemctl stop sprintrelay-worker@codex.service
sudo systemctl start sprintrelay-worker@codex.socket sprintrelay.service
sudo systemctl show sprintrelay-worker@codex.service \
  -p User -p ProtectKernelTunables -p ReadOnlyPaths -p NoNewPrivileges -p IPAddressDeny -p IPAddressAllow
sudo ss -ltn 'sport = :8371'
```

テンプレートの `ProtectKernelTunables=no` は Worker に限定します。systemd が外側のマウント名前空間の `/proc` を保護する設定と、Bubblewrap が入れ子の PID 名前空間に procfs をマウントする操作が競合します。実環境で報告された切り分けは、外側の設定を `no` にするとマウントが通るところまでです。カーネルが拒否した内部条件は未確認です。Worker の `/sys` は systemd で読み取り専用にし、CLI の新しい `/proc` は Bubblewrap で読み取り専用に再マウントします。CLI にはホストの sysfs をマウントしません。Core の `ProtectKernelTunables=yes`、専用 UID、ケーパビリティ制限、ループバック限定と Squid 経由の外部通信は維持します。外側の Worker サービスの proc 保護範囲は以前より狭くなります。[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html) · [mount_namespaces(7)](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html)

### Probe の実行と合格条件

Codex 0.172.0 の追加修正では、合成 Probe の二つの MCP ツールを直接公開し、CLI 内のツール単位の対話承認を事前に指定します。既存サービスへの更新は、レビュー済みソースから `sudo bash deploy/redeploy.sh` を使います。 [0.172.0 の再現とソース調査](codex-probe.md)

1. `/relay status` の `cli_proofs` を記録します。合成 Probe は実データへの同意や `verified=true` を必要としません。`agents.codex.enabled` と `terms_confirmed` が有効で、処理を一時停止中なら `/relay resume` を実行します。
2. 設定済みの承認者がタスクチャンネルで `/relay retry provider:codex` を実行します。両役割のジョブ ID を控えます。片方を確認する場合は `role:management` または `role:audit` を指定します。同じプロバイダーの役割は順番に実行されます。既存ジョブがプロバイダーを使用中ならキューで待機します。ベンダー制限で `cooldown` が返った場合は表示時刻以降に要求します。
3. 別端末で Core と Worker の固定コードを確認します。実行 ID をキーに、初期化 HTTP 200、`MCP_TOOLS_LIST_RESULT http=200 tools=2`、`MCP_GET_PROJECT_CONTEXT`、`MCP_HEARTBEAT_JOB`、`PROBE_COMPLETED` を確認します。CLI 終了コードも診断に使います。
4. `/relay jobs` で両ジョブが `COMPLETED` になり、`/relay status` の `cli_proofs` の `codex / management` と `codex / audit` が今回の実行時刻に更新されたことを確認します。この両方が実アカウントで確認できた時点で疎通試験合格です。診断後は `probe_diagnostics = false` に戻せます。

```sh
sudo journalctl -u sprintrelay.service -u sprintrelay-worker@codex.service \
  --since '15 minutes ago' --grep 'probe code='
```

| 固定コード | 確認できること |
| --- | --- |
| `WORKER_CONNECT`, `WORKER_UNAVAILABLE`, `WORKER_REPLY_INVALID` | Core から Worker ソケットへの送信段階と応答の分類 |
| `SANDBOX_PREPARE_FAILED`, `BINARY_START_FAILED`, `BWRAP_START_FAILED` | マウント・隔離準備、起動失敗、または Bubblewrap の診断シグナル |
| `VERSION_CHECK`, `VERSION_FAILED`, `VERSION_MISMATCH` | バージョン確認実行と結果 |
| `ACCOUNT_CHECK`, `ACCOUNT_AUTH_FAILED` | 公式アカウント認証確認の実行と結果 |
| `CLI_START`, `CLI_EXIT`, `CLI_CONFIG_INCOMPATIBLE`, `CLI_FAILED` | 起動段階、終了コード、設定や引数の非互換性シグナル、その他の異常終了 |
| `INFERENCE_TRANSPORT_FAILED`, `MCP_CLIENT_INIT_FAILED`, `MCP_CLIENT_AUTH_FAILED`, `MCP_CLIENT_LIST_FAILED` | CLI の既知の診断パターンから得た通信・初期化・認証・列挙の候補 |
| `MCP_AUTH_ACCEPTED`, `MCP_AUTH_REJECTED` | サーバーでの Bearer 判定。拒否は未認証なのでジョブ情報なし |
| `MCP_INITIALIZE`, `MCP_TOOLS_LIST`, `MCP_HTTP`, `MCP_RPC_ERROR` | 受信したプロトコル段階、HTTP ステータスと JSON-RPC エラーの有無 |
| `MCP_TOOLS_LIST_RESULT`, `MCP_RESPONSE_UNCLASSIFIED` | 正常な一覧応答のツール数、または一覧応答を分類できなかったこと |
| `CLI_MCP_CALL_STARTED`, `CLI_MCP_CALL_COMPLETED`, `CLI_MCP_CALL_FAILED`, `CLI_MCP_APPROVAL_REQUIRED` | Codex 側のツール呼び出しと承認判定。HTTP 送信前の失敗も区別可能 |
| `CLI_TURN_COMPLETED`, `CLI_TURN_FAILED`, `CLI_FINAL_RESPONSE_PRESENT`, `CLI_FINAL_RESPONSE_ABSENT` | 推論ターンと、完了ターンで観測されたテキスト応答の有無 |
| `CLI_JSON_UNREADABLE`, `CLI_JSON_TRUNCATED`, `CODE_MODE_HOST_UNAVAILABLE` | JSONL を観測できなかった箇所や Code Mode ホスト警告 |
| `MCP_GET_PROJECT_CONTEXT`, `MCP_HEARTBEAT_JOB`, `MCP_OTHER_CALL`, `MCP_TOOL_FAILED` | ツールハンドラー到達とエラーの有無 |
| `CLI_EXITED_WITHOUT_MCP_COMPLETION`, `TIMEOUT`, `PROBE_COMPLETED` | 終了コード 0 での未完了、時間切れ、またはリース検証を経た完了 |

CLI 側の分類は既知の診断文言に基づきます。未分類時は Core 側の到達点で判断します。たとえば `tools/list` まで HTTP 200 でツール呼び出しがなければ、列挙以降に調査範囲を絞れます。ログに診断コード、時刻、相関用ジョブ情報、HTTP ステータス、終了コードを出します。認証拒否のログは診断トークンが有効な間だけ頻度制限付きで出します。CLI の生出力やアカウント情報を取得する追加ログ設定は不要です。Probe の成功は SQLite の完了記録で判断します。
