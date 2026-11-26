# Discord / GitHub コマンドリファレンス

現行 v2 の登録済み Discord コマンドを目的別に整理しています。Discord の入力欄にコマンド名を入力し、表示されるオプションを選択してください。表の「任意」以外は必須です。コマンドは設定済みのギルドとチャンネルで、対応付け済みの利用者として実行します。実際の権限・状態遷移は Core が検証します。

## 会議・仕様（会議チャンネル）

| コマンド | オプション | 用途 |
| --- | --- | --- |
| /meeting import | text または attachment のいずれか、consent（任意）、providers（任意） | 会議処理を明示的に開始 |
| /meeting consent | meeting, providers, confirm_participant_consent（任意） | 承認者が同意済み宛先を変更。none で取り消し |
| /spec draft | meeting, markdown（任意）, minutes（任意） | 初稿ジョブを要求、または Markdown / JSON を手動提出 |
| /spec show | meeting, digest（任意）, view（任意） | review / markdown / minutes を表示・取得 |
| /spec revise | meeting, digest, reason, markdown, minutes（任意） | 人間による査読済み仕様の登録 |
| /spec validate | meeting, digest, report（任意） | 整合性検証を要求、または実際の検証結果 JSON を提出 |
| /spec approve | meeting, digest, tasks（任意）, force（任意）, reason（任意） | 承認者が検証済み版を承認・発行 |
| /scope pending | meeting | 人間の判断が必要な変更を確認 |
| /scope resolve | change_id, accept | 権限を持つ担当者が変更を承認・拒否 |

メッセージのコンテキストメニュー **Apps → Import transcript** からも取り込みを開始できます。

- meeting は会議受付 ID、digest は /spec show で表示される版の識別子。
- 外部 AI への同意は宛先ごとに必要です。providers はカンマ区切りの muse、claude_code、codex、antigravity。
- force:true は査読・検証済みの版に残った指摘を理由付きで受け入れる場合に限ります。公開確認や権限検証を省略するものではありません。
- public リポジトリへ発行する際は公開用のタスク JSON の確認が必要です。[運用手順](../docs/workflow.md)を参照してください。

## タスク（タスクチャンネル）

| コマンド | オプション | 用途 |
| --- | --- | --- |
| /task list | mine（任意） | チーム全体または自分のタスク一覧 |
| /task mine | なし | 自分の担当タスク一覧 |
| /task show | key | 詳細と操作ボタン |
| /task action | key, action, value（任意） | 状態・成果物・見積などを操作 |
| /task claim | key, value（任意） | デザインタスクの担当を引き受ける |
| /task start | key, value（任意） | 作業開始 |
| /task review | key, value（任意） | レビュー依頼 |
| /task done | key, value（任意） | 受け入れ |
| /task block | key, value（任意） | ブロックを登録 |
| /task unblock | key, value（任意） | ブロックを解除 |
| /task estimate | key, value（任意） | 残工数の更新 |

### action の値

ショートカットの value は省略可能な入力欄ですが、操作によって理由・証拠などが必須です。key は Issue 番号ではなく Sprint Relay の安定したタスクキーです。

| action | 実行者 | value / 条件 |
| --- | --- | --- |
| claim | デザイン担当 | 未割当のデザインタスク |
| start | 担当者 | 依存タスクが完了していること |
| link | 担当者 | 成果物または PR の HTTPS URL |
| review | 担当者 | HTTPS URL または既登録の成果物 |
| done | 承認者 | 受け入れの証拠 |
| reject | 承認者 | 修正を求める理由 |
| block | 担当者 | ブロック理由 |
| unblock | 担当者 | ブロック解除 |
| estimate | 担当者 | 残工数（時間）または unknown |
| cancel | 承認者 | キャンセル理由 |

link、reject、cancel は /task action から実行します。

## スケジュールと進捗（タスクチャンネル）

| コマンド | オプション | 用途 |
| --- | --- | --- |
| /schedule show | view（任意） | forecast（既定）/ gantt / critical |
| /schedule recalculate | なし | 予測を再計算しリスク表示 |
| /schedule critical | なし | 依存関係のみのクリティカルパス |
| /schedule settings | なし | 現在のカレンダーを JSON で取得 |
| /schedule update | calendar | 承認者がカレンダー JSON を添付して更新 |
| /progress | なし | 進捗、残工数、リスクを表示 |

例: /schedule show view:gantt で PNG のガントチャートを取得します。critical は依存関係上の期間であり、人員・稼働時間を含む実際の予測とは区別されます。

## AI・サービス状態（タスクチャンネル）

| コマンド | オプション | 用途 |
| --- | --- | --- |
| /relay status | なし | プロバイダー、degraded、巡回時刻 |
| /relay jobs | なし | ジョブ一覧 |
| /relay pause | なし | 承認者が AI 処理を一時停止 |
| /relay resume | なし | 承認者が AI 処理を再開 |
| /relay patrol | なし | 承認者が例外的な巡回を要求 |
| /relay retry | provider, role（任意） | 承認者が合成 Probe を要求 |

retry の provider は codex / claude_code / antigravity、role は management / audit です。既存のレート制限を解除するコマンドではありません。通常の failover は自動です。

## GitHub Issue コメント

対応付け済み GitHub ユーザーは、製品 Issue の新規コメントで次の形式を使えます。

```text
!relay ACTION [VALUE]
```

例:

```text
!relay start
!relay estimate 2
!relay block Waiting for interface definition
!relay unblock
!relay review https://example.org/deliverable
```

Discord と同じ役割・状態遷移の制約が適用され、同期時に処理されます。詳細は [Discord 操作ガイド](../docs/discord.md) を参照してください。

登録名と入力オプションの参照元: [relay/bot.py](../relay/bot.py)。
