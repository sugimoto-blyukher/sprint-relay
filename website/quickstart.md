# クイックスタート

対象は Linux 上で Sprint Relay v2 を運用するチームです。以下は導入の全体像です。資格情報、Worker の隔離、バックアップと復旧については[導入・復旧ガイド](../docs/deployment.md)を先に確認してください。

## 1. 事前準備

- プライベートな管理用 GitHub リポジトリと、製品開発用リポジトリを用意する。
- 管理用と製品用の GitHub 資格情報を分離し、必要最小限の権限だけを与える。
- Discord アプリを作り、対象ギルドに bot と applications.commands のスコープで追加する。
- 会議チャンネルは参加者と Bot のみに制限する。タスク・通知チャンネルを設定する。

詳しくは[外部サービス連携](../docs/integrations.md)を参照してください。GitHub Projects は任意です。

## 2. ローカルで確認する

リポジトリを取得し、そのルートで実行します。実アカウントのトークンは不要です。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m relay check --config config.example.toml
python -m relay doctor --config config.example.toml
python -m relay migrate --dry-run --config config.example.toml
python -m pytest -q
```

## 3. サーバーに導入する

レビュー済みのソースを設置先に取得して実行します。

```sh
sudo bash deploy/setup.sh
```

設定の詳細は [Linux / LXC 導入・復旧](../docs/deployment.md) に従ってください。Core の標準構成は設定が /etc/sprintrelay、状態が /var/lib/sprintrelay、アプリが /opt/sprintrelay です。環境変数や本番設定をコミットしないでください。

AI プロバイダーは各サービスの利用条件、管理者設定、参加者の同意、合成データを用いた受け入れ検証を満たしたものだけ有効にします。

## 4. 最初の会議を処理する

1. 非公開の会議チャンネルで /meeting import を開き、本文または UTF-8 の .md / .txt ファイルを一つ指定する。外部 AI に渡す場合は参加者全員の同意と宛先を指定する。
2. /spec show で会議 ID と処理状況を確認し、仕様の初稿と議事録を取得する。
3. 人間が査読し、/spec revise に会議 ID、digest、理由、査読済み Markdown を渡す。
4. /spec validate と /spec show で同じ digest の整合性検証を確認する。
5. 承認者が /spec approve でその digest を承認する。公開リポジトリには公開用タスク JSON の人手確認が必要。

詳細は[会議から Issue 発行まで](../docs/workflow.md)を参照してください。外部 AI の利用が許可されない場合は手動入力を使いますが、承認前の整合性検証は省略できません。

## 5. 開発中に使う

- /task mine で自分のタスクを確認する。
- /task show で詳細を開き、担当者が開始、成果物のリンク、レビュー依頼などを行う。
- 承認者が受け入れ、または差し戻しを行う。
- /schedule show で予測、view:gantt でガントチャートを表示する。
- /progress で残工数・ブロッカーを確認する。
- /relay status と /relay jobs で管理用 AI の状態を確認する。

操作の引数・権限は[コマンドリファレンス](commands.md)にまとめています。
