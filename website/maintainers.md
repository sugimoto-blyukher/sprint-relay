# ドキュメントサイトの保守

Sprint Relay のサイトは MkDocs / Material for MkDocs でビルドします。既存の README、仕様書、運用ガイドは正本として維持し、サイト用に同じ Markdown を複製して管理しません。

## ローカルプレビュー

リポジトリのルートで実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r website/requirements.txt
python website/prepare.py
mkdocs build --strict
mkdocs serve
```

http://127.0.0.1:8000/ をブラウザで開きます。元の文書を変更したら prepare.py を再実行してください。生成ディレクトリ .docs-src と site はコミットしません。

新しい公開ページを追加するときは website/prepare.py の PAGES と mkdocs.yml の nav を更新します。公開する Markdown のみを明示的に指定してください。認証情報・実データ・状態ディレクトリを追加してはいけません。

## GitHub Pages

1. リポジトリの Settings → Pages → Build and deployment → Source を **GitHub Actions** に設定します。
2. main にマージした後、Actions の **Documentation site** を確認します。必要なら Run workflow から手動実行します。
3. github-pages 環境のデプロイ結果に表示される URL を確認します。

プルリクエストでは厳格なビルド検証のみを行い、main の push または main を選択した手動実行で公開します。公開対象は生成された site ディレクトリのみです。

既定 URL: https://sugimoto-blyukher.github.io/sprint-relay/

GitHub Pages は静的ドキュメントをホストするものであり、Sprint Relay 本体や Discord Bot、MCP サーバーを稼働させるものではありません。
