# SprintRelay

SprintRelay は、5 人のハッカソンチームが Discord で仕様をレビューし、承認した作業を GitHub Issues に登録する Python サービスです。デザイナーは GitHub アカウントを持っていなくても Discord からタスクを引き受け、成果物の URL をレビューに出せます。

## 手元で確認する

Linux / LXC と Python 3.11 以降を使用します。テストと設定チェックには外部サービスの資格情報は不要です。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m relay check --config config.example.toml
python -m pytest -q
python -m pytest -q tests/test_e2e.py
```

E2E テストは架空の議事録を使い、承認、Issue 発行、担当者の操作、日程変更、通知までを模擬環境で再現します。

## セットアップ

1. 管理用の **非公開** GitHub リポジトリと、成果物を開発するリポジトリを用意します。サービスアカウントには管理リポジトリの Contents 読み書き、開発リポジトリの Issues 読み書きと Actions 読み取りを許可します。管理リポジトリのデフォルトブランチにはサービスから直接コミットできるようにします。開発側に公開してよい作業内容だけを発行してください。
2. Projects を使う場合は書き込み可能な Project v2 を作り、Status に `Todo`、`In Progress`、`In Review`、`Blocked`、`Done`、`Cancelled` を登録します。ロードマップを使う場合は Date 型の `Start date` と `Target date` を追加します。Project を設定しなくても Issues と Discord のガントチャートが使えます。
3. Discord Developer Portal でアプリを作り、`bot` と `applications.commands` のスコープで対象サーバーに招待します。指定チャンネルで View Channel、Send Messages、Attach Files、Read Message History を許可します。会議チャンネルは @everyone の閲覧を拒否し、参加者・承認者・Bot に限って閲覧できるようにします。
4. 下記の設定ファイルをコピーします。チーム全員の Discord ID、役割、承認者、サーバー／チャンネル ID、対象リポジトリを実値に置き換えます。GitHub からコマンドを使う参加者には GitHub ログイン名も設定します。開催期間、タイムゾーン、休憩を除いた各人の参加可能時間を入力してください。
5. `.env` に Bot トークンと GitHub トークンを設定して起動します。AI を使う場合の設定、利用条件と接続の検証範囲は [外部連携](docs/integrations.md) を参照してください。

```sh
umask 077
cp config.example.toml config.toml
cp .env.example .env
# config.toml と .env を編集後、同じシェルで実行
set -a
. ./.env
set +a
python -m relay check --config config.toml
python -m relay run --config config.toml --state-dir state
```

常駐させる場合は専用の Linux ユーザーを使い、同じコマンドをサービスマネージャーから起動します。作業ディレクトリ、仮想環境の Python の絶対パス、非公開の環境ファイル、再起動ポリシーを設定してください。1 チーム・1 管理台帳につきサービスは 1 プロセスです。

## ミーティングから完了まで

- 会議チャンネルで `/meeting import` を実行し、本文または UTF-8 のテキストファイルを取り込みます。外部 AI 処理には参加者全員の同意が必要です。
- `/spec draft` の後に人が Markdown を確認し、`/spec revise` で理由とともに保存します。`/spec validate` でその版を検証し、指摘された未解決事項があれば改訂します。承認者が同じ版を `/spec approve` すると、タスクの依存関係を検査して GitHub に発行します。手動入力と外部で実施した AI レビューの取り込み形式は [仕様レビュー手順](docs/workflow.md) にあります。
- タスクチャンネルの `/task show` で担当、依存、受け入れ条件を確認し、ボタンで着手・レビュー依頼を行います。GitHub から操作する場合は対象 Issue に新しいコメントとして `!relay start` などを投稿します。承認者は成果物と条件を確認して完了とします。
- `/schedule show` で予測、クリティカルパス、PNG ガントを表示できます。CPM は依存関係上の最短時間を示し、カレンダー予測には個人の参加時間、兼務の競合、バッファを反映します。予測が立たない作業にはリスクが表示されます。進捗・工数はチームの調整に使います。[Discord 操作一覧](docs/discord.md)

## データ・復旧

GitHub 管理リポジトリの `.sprint-relay/tasks.json` がタスク台帳で、承認済み仕様は同リポジトリの `.sprint-relay/specs/` に保存されます。開発リポジトリの Issue と Projects は台帳の状態を表示します。作業者は Issue コメントまたは Discord の操作を利用します。Issue の直接クローズは同期時に承認済み状態に合わせて調整されます。

議事録、レビュー履歴、同意、発行途中の承認プラン、通知履歴、Discord で更新したカレンダーはローカルの状態ディレクトリに保持します。ディレクトリを所有者限定（0700）にして、停止中にディレクトリ全体を非公開のバックアップ先へ保存してください。復旧時は同じ設定と状態を戻し、サービスを 1 インスタンス起動します。発行途中で停止した場合は同じ会議 ID と承認済みダイジェストで再試行します。Issue は安定キーから回復されます。

GitHub 同期にはポーリングを使います。Discord への通知は送信後に確認済みにするため、送信直後の停止によって同じ通知が再配信されることがあります。
