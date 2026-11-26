# CLI とサービス管理

運営者向けのコマンドです。リポジトリの開発環境または設置先の Python 環境から実行します。通常のチーム作業は [Discord コマンド](commands.md) を使用してください。

## Python CLI

```sh
python -m relay COMMAND [OPTIONS]
python -m relay --help
```

| コマンド | 説明 |
| --- | --- |
| check | 設定・カレンダーの検証 |
| doctor | MCP と AI プロバイダーの設定・接続前提を確認 |
| run | Core を起動（本番資格情報が必要） |
| migrate | SQLite スキーマの移行 |
| backup | SQLite のバックアップを新規ファイルに作成 |
| status | 保存済みジョブ状態を読み出す |

### オプション

| オプション | 説明 |
| --- | --- |
| --config PATH | 設定 TOML。既定は config.toml |
| --state-dir PATH | 状態ディレクトリ。既定は state |
| --dry-run | migrate 専用。メモリ上で移行確認 |
| --output PATH | backup 専用。新規バックアップファイル |
| --report | status 専用。非公開の最新巡回報告も表示 |

### 使用例

```sh
python -m relay check --config config.example.toml
python -m relay doctor --config config.example.toml
python -m relay migrate --dry-run --config config.example.toml
python -m relay status --config config.toml --state-dir state
```

--report の出力には非公開情報が含まれます。運営者の端末で確認し、公開 Issue やサイトに転載しないでください。

## 導入と更新

```sh
sudo bash deploy/setup.sh
sudo bash deploy/redeploy.sh
```

初回導入は setup.sh、更新は redeploy.sh を使用します。更新前に状態と設定をバックアップし、[導入・復旧ガイド](../docs/deployment.md)の手順を確認してください。

## systemd

```sh
sudo systemctl status sprintrelay.service
sudo journalctl -u sprintrelay.service
sudo systemctl is-active sprintrelay.service
```

Codex Worker を利用している場合:

```sh
sudo systemctl status sprintrelay-worker@codex.socket
sudo journalctl -u sprintrelay.service -u sprintrelay-worker@codex.service
```

状態ディレクトリの操作、バックアップ、実際の移行は[導入・復旧ガイド](../docs/deployment.md)に従ってください。同一状態ディレクトリを複数の書き込みプロセスで扱わないでください。

参照元: [relay/__main__.py](../relay/__main__.py)。
