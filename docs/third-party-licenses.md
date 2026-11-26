# 第三者ライセンスの確認

確認日: 2026-11-26

SprintRelay 自体は Apache-2.0 で公開される。以下は依存関係の一次確認であり、全コード・全配布物に対するライセンス監査や権利保証ではない。

## 依存ライブラリ

`pyproject.toml` および `website/requirements.txt` の宣言を対象に、公開されているパッケージのライセンス情報を確認した。バージョン範囲で指定される依存関係について、許容される全バージョンを個別に確認したわけではない。

| パッケージ | 用途 | 主なライセンス | 参照 |
| --- | --- | --- | --- |
| discord.py | 実行 | MIT | [PyPI](https://pypi.org/project/discord.py/) |
| Pillow | 実行 | HPND / MIT-CMU（バージョンによる） | [10.0.0](https://pypi.org/project/pillow/10.0.0/)、[PyPI](https://pypi.org/project/pillow/) |
| pydantic | 実行 | MIT | [PyPI](https://pypi.org/project/pydantic/) |
| mcp | 実行 | MIT | [PyPI](https://pypi.org/project/mcp/) |
| PyJWT | 実行 | MIT | [PyPI](https://pypi.org/project/PyJWT/) |
| cryptography | PyJWT の crypto extra | Apache-2.0 OR BSD-3-Clause | [PyPI](https://pypi.org/project/cryptography/) |
| uvicorn | 実行 | BSD-3-Clause | [PyPI](https://pypi.org/project/uvicorn/) |
| pytest | テスト | MIT | [PyPI](https://pypi.org/project/pytest/) |
| setuptools | ビルド | MIT | [77.0.3](https://pypi.org/project/setuptools/77.0.3/) |
| mkdocs | ドキュメント | BSD-2-Clause | [1.6.1](https://pypi.org/project/mkdocs/1.6.1/) |
| mkdocs-material | ドキュメント | MIT | [9.6.23](https://pypi.org/project/mkdocs-material/9.6.23/) |

上記の主たるライセンスについて、SprintRelay の Apache-2.0 での公開との併用を妨げる条件は一次確認では見つからなかった。各依存ライブラリ自体はその固有のライセンスに従う。ライブラリ、バイナリ、フォント等を同梱・再配布する場合は、実際に配布する版のライセンス本文、著作権表示、NOTICE 等を確認し、必要な表示を保持する。

## 未検証事項

- 実際に解決された全間接依存関係および同梱されるネイティブライブラリのライセンス。
- 生成されたドキュメントサイトに含まれるフォント、JavaScript、画像その他の静的アセットの再配布条件。
- AI 生成コードと第三者の既存コードとの類似性、生成コードの由来、著作権の帰属。リポジトリの著作権表示・SPDX 識別子等の検索だけでは非侵害を確認できない。
- 外部 AI CLI・サービスの利用規約と再配布条件。本体の OSS ライセンスとは別に確認する。

## 保守方針

依存関係や配布形態を変更する際は、実際に解決されたバージョンおよび配布物を基準としてライセンス情報を再確認する。第三者のコードやアセットを取り込む場合は、その出典、ライセンス、必要な帰属表示を記録する。AI を利用して作成した変更も同様とする。

本ファイルは確認記録であり、第三者ライセンス本文や必要な帰属表示を代替するものではない。
