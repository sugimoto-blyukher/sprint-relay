# MCP ツールリファレンス

MCP ツールは AI エージェント用の操作契約です。一般利用者が Discord から直接実行するコマンドとは異なります。

## プロジェクト・ジョブ管理

| ツール | 用途 |
| --- | --- |
| get_project_context | プロジェクトと割当ジョブの情報 |
| list_workflow_jobs | 利用可能なワークフロージョブ一覧 |
| claim_job | ジョブのリースを取得 |
| heartbeat_job | リース更新・合成 Probe |
| report_job_failure | 分類済みの失敗を報告 |

## 会議・仕様・計画

| ツール | 用途 |
| --- | --- |
| get_transcript | 同意済みの文字起こしを取得 |
| submit_minutes | 議事録を提出 |
| get_specification | 議事録・仕様・digest を取得 |
| submit_specification | 仕様案を提出 |
| submit_validation | 整合性検証結果を提出 |
| submit_task_plan | 承認済み版のタスク計画を提出 |

## 巡回・進捗

| ツール | 用途 |
| --- | --- |
| claim_patrol | 巡回スロット取得 |
| get_patrol_delta | 差分のページ取得 |
| get_tasks | タスク情報 |
| get_schedule | スケジュール情報 |
| get_progress | 進捗情報 |
| publish_patrol_report | 巡回報告を保存 |
| complete_patrol | チェックポイントを確定 |

## 技術監査・指摘

| ツール | 用途 |
| --- | --- |
| request_audit | 技術監査を依頼 |
| get_audit_status | 監査状態の取得 |
| get_audit_context | 固定された監査根拠の取得 |
| submit_audit_result | 監査結果の提出 |
| get_audit_result | 完了した監査結果の取得 |
| propose_or_create_issue | 適格な指摘を修正 Issue に反映 |
| post_review_comment | 根拠付きレビューコメントを投稿 |
| update_task_priority | 根拠に基づくタスク優先度変更 |

これらはツール名の索引であり、すべての役割・ジョブに全ツールが公開されるわけではありません。実際の公開範囲・入力スキーマはジョブ、役割、プロジェクト、同意、リースで制限されます。エージェントは接続時の tools/list に従ってください。

- [現行仕様書（MCP インターフェース契約）](../sprint-relay-v2-specification.md)
- [管理用AIへの指示](../docs/agents/management.md)
- [技術監査用AIへの指示](../docs/agents/audit.md)
- [実装上の契約とスキーマ](../relay/mcp_contracts.py)
