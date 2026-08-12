# 翻訳ツール

日本語翻訳ドキュメント (.adoc) の書式を統一するためのツール群です。

## 前提条件

- Python 3.9 以上
- 外部ライブラリ不要 (標準ライブラリのみ使用)

## ツール一覧

### add-jp-lat-spaces.py

日本語文字 (ひらがな・カタカナ・漢字) とアルファベット/数字の間に半角スペースを挿入します。
また、半角括弧 `(` の前および `)` の後にも半角スペースを挿入します。

**変換例:**

| 変換前 | 変換後 |
|---|---|
| `OpenShiftの設定` | `OpenShift の設定` |
| `設定はVMに適用` | `設定は VM に適用` |
| `ステップ1の実行` | `ステップ 1 の実行` |
| `` コマンド`oc get`を実行 `` | `` コマンド `oc get` を実行 `` |
| `仮想マシン(VM)を管理` | `仮想マシン (VM) を管理` |

**処理対象外:**
- コードブロック (`----` / `....` で囲まれた範囲)
- バッククォート内のコンテンツ (境界のスペースは挿入)
- `)` の後が句読点 (`.` `,` `。` `、` 等) や AsciiDoc マークアップ (`*` `>`) の場合はスキップ

### convert-fullwidth-parens.py

全角括弧 `（）` を半角括弧 `()` に変換します。

**変換例:**

| 変換前 | 変換後 |
|---|---|
| `仮想マシン（VM）` | `仮想マシン(VM)` |
| `（必須ではありません）` | `(必須ではありません)` |

**処理対象外:**
- コードブロック (`----` / `....` で囲まれた範囲)

## 使い方

すべてのコマンドはリポジトリルートから実行してください。

### 基本実行 (全ファイル一括処理)

```bash
# 日本語↔アルファベット間のスペース挿入
python3 tools/translation/add-jp-lat-spaces.py

# 全角括弧→半角括弧の変換
python3 tools/translation/convert-fullwidth-parens.py
```

デフォルトでは `modules/` 配下の全 `.adoc` ファイルを処理します。

### プレビュー (dry-run)

変更内容を確認してから適用したい場合は `--dry-run` を使用します。

```bash
python3 tools/translation/add-jp-lat-spaces.py --dry-run
python3 tools/translation/convert-fullwidth-parens.py --dry-run
```

変更箇所の行番号と差分が表示されますが、ファイルは変更されません。

### 特定のファイルやディレクトリを指定

```bash
# 特定のファイルのみ処理
python3 tools/translation/add-jp-lat-spaces.py modules/networking/pages/index.adoc

# 特定のモジュールのみ処理
python3 tools/translation/add-jp-lat-spaces.py modules/networking/

# 複数指定
python3 tools/translation/add-jp-lat-spaces.py modules/storage/ modules/networking/pages/index.adoc
```

## 推奨ワークフロー

新しいページを追加・翻訳した後に以下の順で実行します。

```bash
# 1. プレビューで変更内容を確認
python3 tools/translation/add-jp-lat-spaces.py --dry-run
python3 tools/translation/convert-fullwidth-parens.py --dry-run

# 2. 問題なければ適用
python3 tools/translation/add-jp-lat-spaces.py
python3 tools/translation/convert-fullwidth-parens.py

# 3. ビルド確認
npm run build

# 4. 差分確認
git diff
```

## 注意事項

- 既にスペースが存在する箇所には重複してスペースを挿入しません。同じファイルに対して複数回実行しても安全です。
- AsciiDoc のマクロ (`xref:`, `link:`, `image::` 等) のパス部分は日本語を含まないため影響を受けません。
- バッククォートの数が奇数 (閉じていない) 行は、行全体を通常テキストとして処理します。
