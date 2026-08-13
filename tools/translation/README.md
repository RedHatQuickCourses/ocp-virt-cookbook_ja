# 翻訳ツール

日本語翻訳ドキュメント (.adoc) の upstream 同期と書式整形を行うツール群です。

詳細な仕様は [SYNC-SPEC.md](SYNC-SPEC.md) を参照してください。

## 前提条件

- Python 3.9 以上
- upstream remote の設定:
  ```bash
  git remote add upstream https://github.com/RedHatQuickCourses/ocp-virt-cookbook.git
  ```
- google-genai パッケージと GEMINI_API_KEY 環境変数 (後述)

## クイックスタート

upstream の変更を日本語リポジトリに同期するには、`sync-translate.py` を実行するだけです。ブランチ作成、変更検知、翻訳、書式整形、マニフェスト更新まで全て自動で行われます。

```bash
python3 tools/translation/sync-translate.py --format
```

実行後は `git diff` で翻訳結果をレビューし、問題なければ手動でコミット・プッシュしてください。

## ツール一覧

### sync-translate.py — AI による自動翻訳 (メインツール)

upstream との差分を検出し、AI API で自動翻訳して日本語ファイルに適用する。通常はこのツールだけで全ての同期作業が完了する。

内部的に `sync-check.py` と `sync-mark.py` を自動呼び出しするため、個別に実行する必要はない。`--format` を指定すると書式整形ツール (`add-jp-lat-spaces.py`, `convert-fullwidth-parens.py`) も自動実行される。

**処理内容:**
- main ブランチから翻訳用ブランチを作成
- upstream の変更を検知しマニフェストを更新
- 新規ファイル・モジュールの検出と全文翻訳
- 削除されたファイル・モジュールの削除
- 変更・追加・削除されたブロックの翻訳・挿入・削除
- コードブロック内コメントの英語復元
- 構造不一致 (欠損・余剰ブロック) の自動修正
- nav.adoc / antora.yml の同期
- attachments / images の同期
- マニフェスト・スナップショットの更新

```bash
# デフォルト (Gemini API)
python3 tools/translation/sync-translate.py

# 書式整形も含めて実行 (推奨)
python3 tools/translation/sync-translate.py --format

# dry-run (翻訳結果を表示するが適用しない、ブランチも作成しない)
python3 tools/translation/sync-translate.py --dry-run

# ブランチ名を指定
python3 tools/translation/sync-translate.py --branch translate/2026-08-12

# モデルを指定
python3 tools/translation/sync-translate.py --model gemini-2.5-pro

# 中断した翻訳を再開
python3 tools/translation/sync-translate.py --resume
```

| 引数 | 説明 |
|---|---|
| `--dry-run` | 翻訳結果を stdout に表示するが、ファイルへの書き込み・ブランチ作成を行わない |
| `--format` | 翻訳適用後に `add-jp-lat-spaces.py` と `convert-fullwidth-parens.py` を自動実行 |
| `--model` | Gemini モデル。デフォルト: `gemini-3.6-flash` |
| `--branch` | 作成するブランチ名。デフォルト: `translate/<YYYY-MM-DD>` (実行日) |
| `--resume` | 中断した翻訳を再開する。既存の translate ブランチに切り替え、翻訳済みファイルをスキップして続行する |

### sync-check.py — upstream 変更検知

upstream との差分をブロック単位で検出し、マニフェストのステータスを更新する。`sync-translate.py` が内部的に呼び出すため、通常は単体で実行する必要はない。

翻訳せずに差分の状況だけ確認したい場合に単体実行できる。

```bash
python3 tools/translation/sync-check.py
```

### sync-init.py — スナップショット + マニフェスト初期化

新規ファイルを翻訳管理対象に登録する。upstream の英語原文をスナップショットとして保存し、マニフェストに全ブロックを `synced` として追加する。`sync-translate.py` が新規ファイル検出時に内部的に呼び出す。

```bash
python3 tools/translation/sync-init.py modules/networking/pages/new-page.adoc
```

### sync-mark.py — 翻訳反映済みマーク

翻訳が完了したファイルのスナップショットとマニフェストを最新の upstream 状態に更新する。`sync-translate.py` が処理完了時に内部的に呼び出すため、通常は単体で実行する必要はない。

```bash
python3 tools/translation/sync-mark.py
```

### sync-status.py — 翻訳カバレッジダッシュボード

マニフェストを集計し、翻訳の進捗状況をダッシュボード形式で表示する。

```bash
python3 tools/translation/sync-status.py
```

### validate-structure.py — 構造一致バリデーション

日本語ファイルと upstream 英語ファイルのブロック構造が 1:1 で対応しているかを検証する。欠損や余剰ブロックを検出する。`sync-translate.py` が構造不一致を自動修正するため、修正目的では不要だが、事前確認に使用できる。

```bash
python3 tools/translation/validate-structure.py
```

### add-jp-lat-spaces.py — 日本語↔アルファベット間スペース挿入

日本語文字 (ひらがな・カタカナ・漢字) とアルファベット/数字の間に半角スペースを挿入する。`sync-translate.py --format` で自動実行される。

| 変換前 | 変換後 |
|---|---|
| `OpenShiftの設定` | `OpenShift の設定` |
| `設定はVMに適用` | `設定は VM に適用` |
| `仮想マシン(VM)を管理` | `仮想マシン (VM) を管理` |

```bash
# 単体実行 (全ファイル)
python3 tools/translation/add-jp-lat-spaces.py

# dry-run
python3 tools/translation/add-jp-lat-spaces.py --dry-run

# 特定ファイル/ディレクトリ
python3 tools/translation/add-jp-lat-spaces.py modules/networking/
```

### convert-fullwidth-parens.py — 全角括弧→半角括弧変換

全角括弧 `（）` を半角括弧 `()` に変換する。`sync-translate.py --format` で自動実行される。

```bash
# 単体実行 (全ファイル)
python3 tools/translation/convert-fullwidth-parens.py

# dry-run
python3 tools/translation/convert-fullwidth-parens.py --dry-run
```

## Gemini API の設定

```bash
pip install google-genai
export GEMINI_API_KEY="your-api-key-here"
```

## 推奨ワークフロー

### 定期的な upstream 同期

```bash
# 1. upstream の変更を翻訳 (書式整形込み)
python3 tools/translation/sync-translate.py --format

# 2. 翻訳結果をレビュー
git diff

# 3. ビルド確認
npm run build

# 4. コミット・プッシュ
git add -A
git commit -m "translate: sync with upstream"
git push origin translate/YYYY-MM-DD
```

### 翻訳が中断した場合

API タイムアウトや手動中断 (Ctrl+C) で翻訳が途中で止まった場合、`--resume` で翻訳済みファイルをスキップして再開できます。

```bash
# 中断した翻訳を再開
python3 tools/translation/sync-translate.py --resume

# 書式整形込みで再開
python3 tools/translation/sync-translate.py --resume --format
```

プログレスの粒度はファイル単位です。翻訳途中のファイル (全ブロック完了前に中断) は最初から再翻訳されます。完全にやり直す場合は translate ブランチを削除してから `--resume` なしで実行してください。

### 翻訳せず差分だけ確認したい場合

```bash
python3 tools/translation/sync-check.py
python3 tools/translation/sync-status.py
```

## 注意事項

- `sync-translate.py` 以外のツールは標準ライブラリのみで動作する (外部パッケージ不要)。`sync-translate.py` は `google-genai` のみ必要
- `sync-translate.py` は `git add` / `git commit` / `git push` を一切行わない。翻訳結果はワーキングツリー上の未ステージ変更として残る
- 書式整形ツールは同じファイルに対して複数回実行しても安全 (冪等)
- コードブロック (`----` / `....`) 内は書式整形の対象外
