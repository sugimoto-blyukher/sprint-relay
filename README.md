# SprintRelay v2

SprintRelay は 5 人程度のチーム向けの開発管理サービスです。Discord から取り込んだ議事録を仕様案にし、人が査読して承認した版から GitHub Issues を発行します。GitHub のタスク台帳、Projects、固定担当者の稼働時間を使った予測、ガントチャートは初版から継続して使用できます。

AI 連携には MCP を使います。管理役は Muse → Claude Code CLI → Codex CLI、技術監査は Codex CLI → Antigravity CLI の順で、利用条件・同意・ヘルスを満たす候補だけがジョブを取得します。既定設定では実データをどの外部 AI にも送信しません。全候補が停止している間も、人によるタスク操作、GitHub 同期、スケジュール計算は続きます。[正式仕様書](sprint-relay-v2-specification.md)

## ローカル検証

Linux、Python 3.11 以上を使用します。次の確認には実アカウントもトークンも不要です。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m relay check --config config.example.toml
python -m relay doctor --config config.example.toml
python -m relay migrate --dry-run --config config.example.toml
python -m pytest -q
```

## Linux へのインストール

初回は `sudo bash deploy/setup.sh`、既存環境の更新は `sudo bash deploy/redeploy.sh` を設置先で実行します。Core、Worker、資格情報と再起動後の検証手順はデプロイガイドにあります。 [デプロイガイド](docs/deployment.md)

## 運用を始める

1. プライベートな管理リポジトリと開発リポジトリを分けます。管理用資格情報には管理リポジトリの Contents 書き込み、製品用資格情報には対象リポジトリの Contents / Actions 読み取りと Issues 書き込みを許可します。設定した Project に必要な権限も製品用資格情報に付けます。組織横断時は対象リポジトリへの実際のアクセスを確認してください。
2. Discord アプリを対象サーバーに登録し、会議チャンネルの閲覧を参加者と Bot に限定します。`config.example.toml` をコピーし、リポジトリ、Discord ID、チャンネル、承認者、開催期間、各人の稼働区間を設定します。
3. `.env.example` を所有者限定のファイルにコピーし、異なる管理用・製品用 GitHub 資格情報と Discord Bot トークンを設定します。Core を起動した後、合成データでプロバイダーごとに接続を確認します。会議に使う処理先は、デプロイで許可したうえで取り込み時にも明示します。[デプロイと移行](docs/deployment.md) · [外部サービスと検証状況](docs/integrations.md)
4. 会議チャンネルで `/meeting import` を実行します。仕様が届いたら人が `/spec revise` で査読結果を登録し、同じ digest の整合性検証が通った版を `/spec approve` で承認します。外部 AI を利用できないときは手動ファイルと、対象版の実際の AI チェック結果を入力できます。[Discord 操作](docs/discord.md) · [承認・手動運用](docs/workflow.md)
5. タスク開始・レビュー・受け入れは Discord または Issue コメントから実行します。`/schedule show` で予測・ガント、`/progress` で残工数・ブロッカーを表示し、`/relay status` と `/relay jobs` で巡回・処理状況を確認します。

検証済みの版に未解決の指摘が残った場合、承認者はレポートと diff を確認し、`/spec approve meeting:<ID> digest:<digest> force:true reason:<残存リスクを受け入れる理由>` で進められます。未解決事項を受け入れる責任は承認者にあります。指摘と理由は版ごとに保存され、`/spec show view:review` で確認できます。データ提供の同意、計画の検証、公開リポジトリへの公開確認は引き続き必要です。 [強制承認の運用手順](docs/workflow.md#未解決の指摘を受け入れる場合)

## 状態と権限

GitHub の `.sprint-relay/tasks.json` と `.sprint-relay/specs/` が公開済み状態の正本です。SQLite には版の履歴、同意、ジョブ・リース・チェックポイント、発行や通知の途中状態を保存します。同じジョブを再試行しても Issue を重複発行しないよう、GitHub 側のマーカーと台帳を照合します。Discord 通知は送信直後の異常終了で再配信されることがあります。

MCP の管理権限、監査権限、人の承認権限を別々に検証します。CLI は Core と異なる OS ユーザーで起動します。運用用 AI が取得するのはジョブに限定した権限です。コードを調べるときは SHA に結びついた読み取り情報を使います。[権限境界と復旧](docs/deployment.md) · [エージェント設定](docs/agents/)

合成データで実行する A01〜A16 の対応表と、実アカウントで行う A17 の手順を用意しています。[受け入れテスト](docs/acceptance.md)

Codex の通常ジョブを更新する際は、巡回報告・チェックポイント、技術監査、査読済みの議事録フローを実アカウントで確認してください。[再デプロイとジョブ別の確認手順](docs/deployment.md#codex-通常ジョブの更新と受け入れ)
