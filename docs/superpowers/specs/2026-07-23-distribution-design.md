# 配布改善(GitHub Releases 経由の pip / uv インストール)— 設計ドキュメント

日付: 2026-07-23
対象: `edsdk-python`(maedayoshiaki fork)
ステータス: 設計承認済み・実装前

## 背景と目的

本ライブラリは複数の下流リポジトリ(Densoten ほか今後増える予定)で利用されるが、
現在の導入方法は「所有者がローカルで wheel をビルドし、各リポジトリに wheel ファイルを
手動コピー(vendor 同梱)する」であり、導入・更新の手間が大きい。

一方、Canon EDSDK 本体(EDSDK.dll / EdsImage.dll / ヘッダ / .lib)は再配布できない。
このため「pip で配布できるのか」が論点だったが、現状のリポジトリ構造を確認した結果:

- Canon 由来ファイルは `dependencies/` 配下で gitignore されており、リポジトリに含まれない
- ビルド済み wheel に入るのは自作コード(.pyd は EDSDK.dll を実行時に動的ロードするのみ)
- 利用者が Canon SDK を別途入手し `EDSDK_PYTHON_DLL_DIR` で指定する運用が確立済み

つまり **wheel 自体の配布は Canon のコード再配布に当たらない構造がすでにできている**。
残る課題は配布経路のみであり、本設計は GitHub Releases を配布チャネルとして
`pip install <URL>` / `uv sync` だけで導入できるようにする。

## リサーチ結果の要約

- **先行事例**: EDSDK ラッパーの公開レジストリ配布は複数の前例がある
  (Java ラッパー Blackdread/canon-sdk-java は Maven Central、Node.js ラッパー
  napi-canon-cameras は npm)。いずれも「Canon ファイル非同梱・利用者が Canon
  開発者プログラムから SDK を入手」方式。Azure Kinect の pyk4a(PyPI 公開 +
  環境変数 `K4A_DLL_DIR` で DLL 指定)が最も近いモデルで、本リポジトリの
  `edsdk/__init__.py` の DLL 探索実装はすでに同じベストプラクティスに沿っている。
- **ライセンス上の注意**: 公開されている Canon 開発者契約(2013 年シンガポール版
  EDSDK v2.14)には「SDK を利用した開発ツールの開発禁止」(§5)と読める条項があり、
  字義通りにはラッパー配布全般とテンションがある。コミュニティ慣行では 10 年以上
  多数のラッパーが公開されたまま問題化しておらず事実上容認されているが、ゼロリスク
  ではない。GitHub Releases は PyPI より露出が低く、この点でも無難な選択。
  **Canon のファイルを一切同梱しない原則は絶対に維持する**(CI やプライベート
  ストレージへの SDK 預け入れも契約グレーのため行わない = ビルドは所有者ローカルのみ)。
- **技術確認**: 公開リポジトリの Release 資産は PEP 508 直接参照
  (`edsdk-python @ https://github.com/.../releases/download/.../xxx.whl`)で
  pip / uv からインストール可能。Python バージョンごとの環境マーカーを付けて
  複数 wheel を列挙すれば自動選択される。

## 方針(ユーザー決定事項)

- 配布チャネルは **GitHub Releases**(将来の PyPI 移行は妨げない)
- 対応 Python は **3.11 / 3.12 / 3.13** の 3 wheel(win_amd64)
- 初回リリース(v0.1.7)まで実施する

## 設計

### 1. リリース自動化スクリプト `scripts/release.py`

所有者のマシンで実行する前提(Canon SDK・MSVC・uv・gh CLI が揃っている環境)。
標準ライブラリのみ使用(`tomllib`, `argparse`, `subprocess`, `pathlib` など)。

CLI:

```
python scripts/release.py [--dry-run] [--skip-smoke] [--pythons 3.11 3.12 3.13]
                          [--repo OWNER/NAME] [--allow-branch]
```

- `--dry-run`: ビルドと検証のみ。タグ作成・GitHub Release 作成を行わない
- `--skip-smoke`: wheel インストール検証を省略(デバッグ用)
- `--pythons`: 対象バージョン(デフォルト `3.11 3.12 3.13`)
- `--repo`: リポジトリスラッグ(デフォルト `maedayoshiaki/edsdk-python`)
- `--allow-branch`: main 以外のブランチからの実行を許可(デフォルトは main のみ。
  `--dry-run` 時はブランチチェック自体を行わない)

処理フロー:

1. **前提チェック**(失敗項目を全て列挙してから終了):
   - `dependencies/EDSDK/Header/EDSDK.h`、`dependencies/EDSDK_64/Library/EDSDK.lib`、
     `dependencies/EDSDK_64/Dll/EDSDK.dll`、`dependencies/EDSDK_64/Dll/EdsImage.dll` の存在
   - `uv` / `gh` コマンドの存在(`gh auth status` は release 作成直前に確認)
   - `git status --porcelain` が空(クリーンな作業ツリー)
   - リリース時のみ: 現在ブランチが main(`--allow-branch` で回避可)、
     タグ `v{version}` がローカル・リモート双方に未存在
2. **バージョン取得**: `pyproject.toml` の `project.version` を `tomllib` で読む
3. **ビルド**: 対象 Python ごとに `uv build --wheel --python {ver}` を実行し、
   `dist/edsdk_python-{version}-cp3XX-cp3XX-win_amd64.whl` の生成を確認
   (ビルド前に dist/ 内の当該バージョン wheel を削除)
4. **スモークテスト**(wheel ごと): 一時ディレクトリに `uv venv --python {ver}` で
   venv を作成 → `uv pip install {wheel} pytest` → `tests/test_exposure.py` を
   一時ディレクトリへコピー → `EDSDK_PYTHON_DLL_DIR=dependencies/EDSDK_64/Dll` を
   設定して venv の python で `pytest test_exposure.py -q` を実行し全件合格を確認。
   一時ディレクトリで実行するのは、リポジトリ直下の `edsdk/` ソースツリーが
   インストール済みパッケージをシャドウするのを防ぐため
5. **リリース作成**(`--dry-run` 以外): `gh release create v{version} dist/*.whl
   --title "v{version}" --notes {生成ノート}`
6. **消費側スニペット出力**: 実 URL 入りの pyproject.toml 依存ブロックを標準出力に
   表示(リリースノートにも同内容を含める)

純粋関数として切り出しユニットテスト対象とするもの:

- `parse_project_version(pyproject_text) -> str`
- `wheel_filename(version, python) -> str`(例: `("0.1.7", "3.11")` →
  `edsdk_python-0.1.7-cp311-cp311-win_amd64.whl`)
- `consumer_snippet(repo, version, pythons) -> str`(環境マーカー付き依存ブロック)
- `release_notes(repo, version, pythons) -> str`
- `check_sdk_files(repo_root) -> list[str]`(不足ファイルのリストを返す)

### 2. README への消費側ガイド追加

`## Exposure control` の前に `## Installing from GitHub Releases (for consumer projects)`
セクションを追加:

- pyproject.toml 依存ブロックの例(v0.1.7 の実 URL、3 バージョンの環境マーカー付き)
- `uv sync` / `pip install` だけで導入できること
- Canon SDK 本体は従来どおり各自 Canon 開発者プログラムから入手し
  `EDSDK_PYTHON_DLL_DIR` を設定すること(wheel には Canon のコードは含まれない旨を明記)
- 従来の wheel 手動同梱方式も引き続き動作すること

また保守者向けに `## Releasing (maintainers)` セクションを追加し、
`python scripts/release.py --dry-run` → main へマージ → `python scripts/release.py`
の手順を記載する。

### 3. ImportError メッセージの改善(小変更)

`edsdk/__init__.py` の ImportError メッセージ末尾に、Canon 開発者プログラムから
SDK を入手する旨の一文を追加する(先行事例 edsdk4j の「apply for a download
yourself」慣行に合わせる)。

### 4. バージョン

`pyproject.toml` を `0.1.7` に bump(配布ツーリング追加バージョン)。

## 互換性

- ライブラリ本体のコード変更は `edsdk/__init__.py` のエラーメッセージ 1 文のみ。
  API・挙動の変更なし
- Densoten の wheel 手動同梱方式・環境変数運用はそのまま動作(移行は任意)

## テスト計画

- `tests/test_release_script.py`(カメラ・SDK 不要): 上記純粋関数のユニットテスト
  (バージョン解析、wheel ファイル名、スニペット生成、SDK ファイルチェックの
  tmp_path フィクスチャによる検証)
- 統合検証: 所有者マシンで `python scripts/release.py --dry-run` を実行し、
  3 wheel のビルドとスモークテスト全合格を確認(実装タスク内で実施)
- 実機カメラは不要

## スコープ外(将来オプション)

- PyPI 公開(名前空間確保・運用整備後に検討。技術的には本設計の wheel をそのまま
  アップロード可能)
- abi3(Py_LIMITED_API)化による単一 wheel 化
- GitHub Pages の PEP 503 簡易インデックス(導入先が増えて URL 列挙が煩雑になったら)
- CI(GitHub Actions)でのビルド(Canon SDK を CI に置くのは契約上グレーのため見送り)
