# Codex 0.172.0 と合成 Probe

## 再現結果

Linux x86_64 向け公式リリース `rust-v0.172.0` の CLI を使用した。配布アーカイブの SHA-256 を GitHub Releases の値と照合した。推論先にはループバックの模擬モデルを設定し、MCP サーバー、Streamable HTTP、ジョブ資格情報、SQLite は SprintRelay の実装を使用した。模擬モデルは受け取ったツール定義を検査し、指定された合成呼び出しを返す。運用者がその後、Linux 実環境で両役割の合成 Probe 完了と証跡の保存を確認した。 [公式リリース](https://github.com/openai/codex/releases/tag/rust-v0.172.0)

| CLI の設定 | モデル向けの直接公開 | 模擬モデルによる Probe 結果 |
| --- | --- | --- |
| 旧設定 | 2 件の `tools/list` が成功。直接定義は 0 件 | MCP 呼び出しなし。CLI は終了 0 |
| 直接公開のみ追加 | `mcp__sprintrelay` にチャレンジ取得・応答の 2 件 | CLI 内で両方が承認待ちに分類され、HTTP 送信前に失敗 |
| 直接公開とツール単位の承認 | 同じ 2 件 | 管理役・技術監査役とも両ツールを実行し、`COMPLETED` と `cli_proof` を保存 |

旧設定で報告されたログと同じ停止点を再現した。実環境で使われたモデル、モデルカタログ、最初の `tools/list` 応答は記録されていなかったため、当時の実行が同じ経路だったかは未確認。

## 公式ソースで確認した経路

1. Codex は設定された HTTP MCP サーバーを初期化してツールカタログを取得する。`required=true` は初期化の成否を起動に反映し、`enabled_tools` は登録する名前を絞る。ツールをモデルへ公開する段階ではツールの入力スキーマを変換し、出力スキーマをツール定義へ取り込む。正規の 0.172.0 バイナリで、今回の 2 スキーマからモデル向け定義が生成され、`get_project_context` の結果が模擬モデルへ届き、`heartbeat_job` が実行されることを確認した。 [MCP 設定型](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/config/src/mcp_types.rs#L243-L297) · [MCP スキーマ変換](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/tools/src/mcp_tool.rs#L24-L77)
2. モデルがツール検索に対応すると、通常の MCP ツールも既定で遅延公開される。さらにモデルカタログに `tool_mode` があれば、機能フラグより優先される。バンドル版の GPT-6.2 Sol は `code_mode_only` を指定する。ホストが見つからない場合、ソース上 `CodeMode` には直接ツールへのフォールバックがあるが、`CodeModeOnly` はその条件に含まれない。実験でも `Code mode will fail closed` の固定パターンを確認した。 [MCP 公開既定値](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/mcp_tool_exposure.rs#L133-L165) · [モデルと実効モード](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/tools/mod.rs#L69-L90) · [同梱モデルカタログ](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/models-manager/models.json) · [ホスト警告](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/tools/code_mode/mod.rs#L109-L125)
3. 合成 Probe では、`omit_tools_from=["deferred","code_mode"]` で 2 ツールを直接公開する。GPT-6.2 Sol の最初の模擬モデル要求では `additional_tools` に載った。Codex の管理ポリシーで `code_mode_only_strict_3p_tools` が有効なら公開設定より優先される。そのポリシーを変更して回避する手順は含めていない。 [公開ポリシー](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/tools/spec_plan.rs#L229-L319) · [運用設定リファレンス](https://developers.openai.com/codex/config-reference)
4. 設定した MCP サーバーの承認ポリシーは、HTTP 送信より前に評価される。`approval_policy="never"` では対話承認が必要な呼び出しが拒否される。固定の 2 ツールだけに合成 Probe 用の `approval_mode="approve"` を設定した。有効な Bearer・リース・役割と正しいチャレンジ応答は引き続きサーバーが検証する。通常ジョブは役割と用途別にツールを選び、仕様書の人間承認は Core が検証する。 [非対話実行の承認判定](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/mcp_tool_call.rs#L1894-L1933) · [ツール単位の承認モード](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/mcp_tool_call.rs#L2908-L2919)

## Worker の無効化フラグ

| フラグ | 通常の HTTP MCP 登録との関係 |
| --- | --- |
| `shell_tool`, `unified_exec`, `js_repl` | シェル、PTY、旧 JavaScript 実行機能。HTTP MCP 登録に不要。`js_repl` は 0.172.0 では削除済み互換キー |
| `hooks`, `plugins`, `skill_mcp_dependency_install` | フック、プラグイン、スキル依存の自動導入。設定済み HTTP サーバーは個別にロードされる |
| `apps` | Codex Apps 専用の経路。一般 MCP は `non_app_tools` から登録される |
| `browser_use`, `computer_use`, `in_app_browser` | ブラウザー、コンピューター、内蔵ブラウザー連携。今回の登録経路を制御しない |
| `multi_agent_v2`, `request_permissions_tool` | エージェント間連携と権限要求の組み込みツール |
| `code_mode` | モデルにモード指定がない場合の既定値に影響。明示された `code_mode_only` を上書きしない |

既存の無効化フラグは保持した。二つの合成ツールは通常の MCP 関数として直接呼べるため、Code Mode ホストを追加する必要はない。 [0.172.0 の機能一覧](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/features/src/lib.rs) · [一般 MCP と Apps の分岐](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/mcp_tool_exposure.rs#L149-L159)

## 診断と再現方法

`http=-` は MCP SDK の `HTTPStatus` が `int` のサブクラスで、診断の型判定が `type(status) is int` だったことが原因。ステータスを整数に正規化する。JSON-RPC 本文はサイズを制限して解析し、`MCP_TOOLS_LIST_RESULT http=200 tools=2`、`MCP_RPC_ERROR`、`MCP_RESPONSE_UNCLASSIFIED` の形で記録する。HTTP 200 はクライアント側での登録や実行を保証しない。

該当する診断設定を有効化した Codex 起動時に `--json` を渡す。固定コードとして呼び出しの開始・完了・失敗と、承認拒否および Code Mode ホスト警告を記録する。`CLI_FINAL_RESPONSE_PRESENT` は完了ターンで内容のある `agent_message` が観測されたことを示す。CLI の JSONL には独立した最終メッセージ種別がない。チャレンジ、トークン、引数、応答本文、JSONL の生データは記録しない。 [Codex 非対話実行](https://developers.openai.com/codex/noninteractive) · [0.172.0 の JSONL イベント定義](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/exec/src/exec_events.rs)

確認済みの 0.172.0 ネイティブバイナリを使う場合は、次の任意テストを実行できる。外部の推論サービスや実アカウントを使用しない。

```sh
SPRINTRELAY_TEST_CODEX=/absolute/path/to/codex python -m pytest -q tests/test_codex_probe_cli.py
```

実機では `sudo bash deploy/redeploy.sh` で更新し、診断を有効にして両役割を再試行する。ジョブ ID ごとに `MCP_TOOLS_LIST_RESULT http=200 tools=2`、二つのツールの完了、`PROBE_COMPLETED` を確認する。合格は、実アカウントで今回の両ジョブが `COMPLETED` となり、`cli_proofs` の両役割の時刻が更新されたときとする。 [再デプロイと運用手順](deployment.md#worker-更新と-codex-の実環境受け入れ)

## 通常ジョブでの比較試験

公式 CLI 0.172.0、GPT-6.2 Sol のモデルカタログ、ループバックの模擬推論を使って巡回ジョブを比較した。サーバーには有効なジョブ資格情報を渡し、各ケースで同じ巡回用カタログを返した。旧起動引数ではモデルに渡った直接公開の SprintRelay 関数は 0 件だった。直接公開だけを適用すると、模擬モデルが選んだ `get_project_context` が CLI 内で対話承認を要求され、HTTP 呼び出しに至らなかった。どちらも終了コード 0 で SQLite 上は RUNNING のままだった。実環境で発生した通常ジョブの FORMAT は当時の診断がなく、同じ経路だったかは特定できない。

Core が取得済みのリースに対応するジョブを Worker に渡し、Codex の直接公開と個別承認の両方に同じ許可リストを使う。サーバーは SQLite のリースからジョブ種別を再判定する。Probe は従来の 2 ツールのみ。通常ジョブにはコンテキスト取得、リース更新、分類済み失敗の報告に加え、次の操作を公開する。

| ジョブ | 用途別の操作 |
| --- | --- |
| 議事録 / 仕様書 | 同意済みの議事録取得と抽出結果提出 / 議事録・版情報取得と仕様書提出 |
| 査読後の検証 / 承認後の計画 | 現行版取得、必要な技術監査の依頼と検証提出 / 承認版取得と計画提出 |
| 巡回 | 差分ページと進捗・予定・タスク取得、監査依頼と結果取得、根拠付きの Issue・コメント・優先度処理、報告とチェックポイント |
| 技術監査 | 指定された証拠の取得と監査結果提出。仕様監査では対象の仕様書全文も取得可能 |

修正後のネイティブ試験では、モデルが受けたツール名・説明・入力スキーマと、実際のツール応答に含まれるリース、巡回カーソル、監査根拠 ID、仕様 digest の受け渡しを検査する。模擬推論はあらかじめ決めた操作を返す。実モデルが自律的に選ぶ操作は実環境で確認する。[テスト](../tests/test_codex_jobs.py) · [実環境での受け入れ](deployment.md#codex-通常ジョブの更新と受け入れ)
