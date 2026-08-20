# 翻訳同期管理ツール 仕様書

## 1. 目的と前提条件

### 1.1 目的

upstream リポジトリ (`RedHatQuickCourses/ocp-virt-cookbook`) の英語コンテンツが更新された際に、日本語翻訳リポジトリ側で **どのファイルのどのブロック (段落) を更新すべきか** を自動検知し、翻訳の反映漏れを防止する。

### 1.2 方式概要

- upstream を第2 git remote として追加し、英語コンテンツの最新状態を常にローカルに保持する
- 翻訳時点の英語原文を **スナップショット** として `tools/translation/originals/` に保存する
- スナップショットと upstream の現在の英語を **ブロック単位** で比較し、変更を検知する
- 変更があったブロックを **不変アンカー照合** で日本語ファイル内の該当行にマッピングする
- 翻訳状況は **マニフェスト JSON** でブロック単位に管理する

### 1.3 運用ルール

翻訳者は以下のルールを遵守する:

1. **段落の統合禁止** — 英語の2段落を日本語の1段落にまとめない
2. **段落の分割禁止** — 英語の1段落を日本語の複数段落に分けない
3. **セクション・段落の並べ替え禁止** — 英語と同じ順序を維持する
4. **全ブロック一括反映** — `sync-check.py` で変更が検知されたファイルは、全ての `outdated` / `new` ブロックを翻訳に反映してから `sync-mark.py` でマークする。部分的なマークは行わない

ルール 1-3 により、英語スナップショットと日本語ファイルのブロック構造が 1:1 で対応し、位置ベースの照合が確実に機能する。ルール 4 により、スナップショットとマニフェストの整合性が常に保たれる。

### 1.4 前提条件

- Python 3.9 以上 (標準ライブラリのみ使用)
- upstream remote が設定されていること:
  ```bash
  git remote add upstream https://github.com/RedHatQuickCourses/ocp-virt-cookbook.git
  git fetch upstream
  ```

---

## 2. ブロック解析アルゴリズム

### 2.1 ブロックの定義

**ブロック** とは、AsciiDoc ファイル内の意味的に独立した最小単位である。ファイルはブロックの線形シーケンスとして解析される。空行はブロック区切りとして扱い、ブロック自体には含めない。

### 2.2 ブロック種別

| ブロック種別 | 開始パターン | 終了条件 | 翻訳対象 |
|---|---|---|---|
| `document_header` | 1行目の `= タイトル` | 連続する `:attr:` 行の末尾まで | タイトルと一部属性値 |
| `section_header` | `^={2,5}\s` | 単一行 | 見出しテキスト (用語集に一致すれば用語集を優先) |
| `prose` | 他の種別に該当しない非空行 | 空行または他の種別の開始 | 全文 |
| `code_block` | `[source,...]\n----` または単独の `----` | 対応する `----` 閉じデリミタ | コメント行のみ翻訳 (コード本体は不変) |
| `literal_block` | `....` | 対応する `....` 閉じデリミタ | 不変 |
| `admonition_inline` | `^(NOTE\|WARNING\|IMPORTANT\|CAUTION\|TIP): ` | 空行 | 本文 (キーワードは不変) |
| `example_block` | `[NOTE]\n====` 等、または単独の `====` | 対応する `====` 閉じデリミタ | 内容 (アドモニションキーワードは不変) |
| `table` | `\|===` | 対応する `\|===` 閉じデリミタ | セル内容 |
| `list_item` | `^(\*+\|\.+)\s` または `^.+::` | 空行または同レベル以上の次リスト項目 | テキスト部分 |
| `block_title` | `^\.[A-Za-z　-鿿]` (`.` + 非空白、`....` ではない) | 単一行 | 全文 |
| `block_attribute` | `^\[.*\]$` (source/cols 等の属性行) | 単一行 | 不変 |
| `attribute_entry` | `^:[a-zA-Z].*:` | 単一行 | 値のみ (キーは不変) |

### 2.3 グルーピングルール

- **block_attribute 行** (`[source,yaml]`, `[cols="1,2"]`, `[NOTE]`, `[IMPORTANT]` 等) は、直後のデリミタブロック (code_block, table, example_block 等) と **一体のブロック** として扱う。単独ブロックにはしない。
- **block_title 行** (`.期待される出力` 等) も同様に、直後のブロックの一部として扱う。
- **リスト継続** (`+` 単独行) は、前のリスト項目ブロックに含める。`+` に続くコードブロック、リテラルブロック、テーブル (`|===`) 等のデリミタブロックも同じリスト項目ブロックの一部とする。

### 2.4 解析ステートマシン

```
状態: NORMAL
  行を読む:
    空行               → 現在のブロックを確定、NORMAL のまま
    ^={1,5} テキスト   → 現在のブロックを確定、section_header を確定
    ^[source,...]      → block_attribute を蓄積、EXPECT_DELIMITED へ
    ^[NOTE] 等         → block_attribute を蓄積、EXPECT_DELIMITED へ
    ^----\s*$          → IN_CODE_BLOCK へ (block_attribute があれば付与)
    ^....\s*$          → IN_LITERAL_BLOCK へ
    ^====\s*$          → IN_EXAMPLE_BLOCK へ (block_attribute があれば付与)
    ^|===\s*$          → IN_TABLE へ (block_attribute があれば付与)
    ^[cols=...]        → block_attribute を蓄積、EXPECT_TABLE へ
    ^\[.*\]$ (上記以外)→ block_attribute を蓄積、EXPECT_DELIMITED へ
    ^NOTE: 等          → admonition_inline ブロック開始、NORMAL のまま
    ^.テキスト         → block_title を蓄積、EXPECT_TITLED_BLOCK へ
    その他             → prose または list_item ブロックに蓄積

状態: IN_CODE_BLOCK
  ^----\s*$ (対応する閉じ) → ブロック確定、NORMAL へ
  その他                   → code_block に蓄積

状態: IN_LITERAL_BLOCK
  ^....\s*$ (対応する閉じ) → ブロック確定、NORMAL へ
  その他                   → literal_block に蓄積

状態: IN_EXAMPLE_BLOCK
  ^====\s*$ (対応する閉じ) → ブロック確定、NORMAL へ
  その他                   → example_block に蓄積

状態: IN_TABLE
  ^|===\s*$ (閉じ)        → ブロック確定、NORMAL へ
  その他                   → table に蓄積

状態: EXPECT_DELIMITED
  ^----\s*$            → IN_CODE_BLOCK へ (蓄積済み block_attribute を付与)
  ^====\s*$            → IN_EXAMPLE_BLOCK へ (蓄積済み block_attribute を付与)
  ^.テキスト            → block_title も蓄積、EXPECT_DELIMITED のまま
  その他                → block_attribute を単独ブロックとして確定、行を再処理

状態: EXPECT_TITLED_BLOCK
  ^----\s*$            → IN_CODE_BLOCK へ (蓄積済み block_title を付与)
  ^====\s*$            → IN_EXAMPLE_BLOCK へ (蓄積済み block_title を付与)
  ^....\s*$            → IN_LITERAL_BLOCK へ (蓄積済み block_title を付与)
  ^|===\s*$            → IN_TABLE へ (蓄積済み block_title を付与)
  ^[source,...] 等     → block_attribute も蓄積、EXPECT_DELIMITED へ
  ^[NOTE] 等           → block_attribute も蓄積、EXPECT_DELIMITED へ
  ^\[.*\]$ (上記以外)  → block_attribute も蓄積、EXPECT_DELIMITED へ
  その他                → block_title を単独ブロックとして確定、行を再処理
```

**注意**: デリミタ行 (`----`, `....`, `====`, `|===`) は末尾に空白文字が付いていても有効なデリミタとして扱う (`\s*$`)。upstream のファイルに末尾空白付きデリミタが存在するケースが確認されている (例: `creating-managing-storage-class.adoc` L82)。

**リスト継続中のデリミタブロック**: `+` (リスト継続) の後に出現する `----`, `....`, `|===` はそれぞれ対応する閉じデリミタまでを list_item ブロック内に含める。テーブル (`|===`) も同様に、開き・閉じデリミタとその間の全行を list_item ブロックの一部として蓄積する。

**EOF でのブロック確定**: ファイル末尾に到達した時点で IN_CODE_BLOCK, IN_LITERAL_BLOCK, IN_EXAMPLE_BLOCK, IN_TABLE の各状態にある場合は、蓄積中の内容を対応するブロック種別 (code_block, literal_block, example_block, table) として確定する。内容をサイレントに破棄してはならない。

### 2.5 このリポジトリで確認されたパターンへの対応

| パターン | 出現例 | 処理 |
|---|---|---|
| ラベルなし `----` ブロック | `cpu-pinning.adoc` L85-88 | `code_block` として扱う (block_attribute なし) |
| `<<EOF` ヒアドキュメント内の YAML | `cpu-pinning.adoc` L59-74 | `----` 内部は解析しない。全体が1つの code_block |
| `.期待される出力` ブロックタイトル | `static-ip-configuration.adoc` L276 | block_title として直後のブロックに付与 |
| 定義リスト (`xref:...[]::`) | `getting-started/pages/index.adoc` L28-47 | `list_item` として処理 |
| `[cols=...,options="header"]` テーブル | `cpu-pinning.adoc` L29-40 | block_attribute + table を一体ブロック化 |
| 番号付きリスト (`. テキスト`) | `performance/pages/index.adoc` L42-45 | `list_item` (`.` の後に空白で判定) |
| `[IMPORTANT]` / `[NOTE]` + `====` ブロック | `linux-bridges.adoc` L49-54, `api-component-overview.adoc` L533-537 | block_attribute + example_block を一体ブロック化。`====` は `^====\s*$` で判定 (テキスト付きの `==== 見出し` はセクションヘッダー) |
| block_attribute + block_title + `====` ブロック | `layer2-secondary.adoc` L184-202 | block_attribute (`[NOTE]`) + block_title + example_block を一体ブロック化 |
| `[#anchor-id]` ショートハンドアンカー | `installing-qemu-guest-agent.adoc` (14箇所), `hotplug-volumes-interfaces.adoc` (2箇所) 等 | block_attribute として蓄積。直後がデリミタブロックでなければ単独ブロックとして確定 |
| `[[anchor-id]]` スタンドアロンアンカー | `lvm-operator.adoc` L287, `dual-stack-vms.adoc` L391 | 同上。`^\[.*\]$` にマッチするため block_attribute として処理 |
| `[start=N]` リスト継続属性 | `install-openshift-virtualization.adoc` L146 | 同上。直後のリスト項目に対する属性 |

---

## 3. ブロック ID 体系

### 3.1 設計方針

ブロック ID は **行番号に依存せず**、ファイル内容から決定論的に生成する。軽微な編集 (誤字修正等) で ID が変化しないようにする。

### 3.2 ID 構成

```
<section_path>/<block_type>/<ordinal>
```

- **section_path**: ブロックを囲むセクション見出しの階層パス。アンカー ID (`[[anchor_id]]`) があればそれを使用し、なければ見出しテキストの slug 化した文字列を使用する。最初のセクション見出し以前のブロックは `_root` とする。
- **block_type**: 2.2 で定義した種別名
- **ordinal**: 同一セクション内で同一種別のブロックの出現順序 (0始まり)

### 3.3 具体例

`modules/performance/pages/cpu-pinning.adoc` の場合:

```
_root/document_header/0           ← L1-2: "= 仮想マシン向けの専用 CPU 配置" + ":navtitle:"
概要/prose/0                      ← L6-8: 概要の第1-2段落
前提条件/list_item/0              ← L12: "* OpenShift Virtualization..."
前提条件/list_item/1              ← L13: "* クラスター管理者..."
...
専用-cpu-配置の理解/prose/0        ← L20-22: 説明段落
専用-cpu-配置の理解/prose/1        ← L24-27: 要件リスト前の段落
専用-cpu-配置の理解/table/0        ← L29-40: 比較テーブル
ステップ-1-cpu-manager.../code_block/0  ← L48-51: 最初のコマンド
ステップ-1-cpu-manager.../admonition_inline/0  ← L53: NOTE:
```

### 3.4 slug 化ルール

セクション見出しテキスト (英語または日本語) を ID に変換する際のルール:

1. `[[anchor_id]]` がある場合はそれをそのまま使用 (最優先)
2. ない場合、見出しテキストから:
   - 先頭の `=` 記号とスペースを除去
   - 全角英数字を半角に変換
   - 小文字化
   - 空白を `-` に置換
   - `[a-z0-9぀-鿿-]` 以外の文字を除去
   - 50文字で切り詰め

### 3.5 安定性の特性

| 変更内容 | ID への影響 |
|---|---|
| ブロック内の文章修正 | 影響なし (ID はブロックの位置と種別で決まる) |
| 同一種別のブロック追加 | 追加位置以降の ordinal がずれる |
| セクション見出し変更 | そのセクション内の全 ID が変わる |
| 異なる種別のブロック追加 | 影響なし (ordinal は種別ごと) |

ordinal のずれは `sync-check.py` がハッシュ比較で検知するため、実運用上の問題にはならない (後述のセクション 7.2 参照)。

---

## 4. ハッシュ計算

### 4.1 対象

各ブロックの **全行の生テキスト** をハッシュ対象とする。block_attribute や block_title がグルーピングされている場合は、それらの行も含める。

### 4.2 正規化

```python
def compute_block_hash(lines: list[str]) -> str:
    normalized = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
```

- 各行の末尾空白を除去
- 改行 (`\n`) で結合
- SHA-256 の先頭16文字 (64bit) を使用
- 約10,000ブロック規模での衝突確率は無視できる水準 (~1/10^14)

### 4.3 ハッシュ対象外

- ブロック間の空行 (構造的区切りであり内容ではない)
- ブロック ID そのもの
- ファイルパスや行番号などのメタ情報

---

## 5. 不変アンカー照合アルゴリズム

### 5.1 目的

英語スナップショットのブロックと日本語ファイルのブロックを対応づけ、変更があった英語ブロックに対応する **日本語側の行番号** を特定する。

### 5.2 不変アンカーの定義

英語と日本語の間で **内容が同一または構造的に同一** な要素:

| 要素 | 不変部分 | フィンガープリント |
|---|---|---|
| セクション見出し | `=` レベル + `[[anchor_id]]` (あれば) | `("section", level, anchor_id_or_None)` |
| コードブロック | コード本体 (コメント行を除くコード行) | `("code", content_hash[:16])` |
| block_attribute | `[source,yaml]` 等の全文 | `("attr", raw_text)` |
| admonition キーワード | `NOTE:`, `WARNING:` 等 | `("admonition", keyword)` |
| テーブル構造 | `[cols=...]` 属性 + `\|===` デリミタ | `("table", cols_attr)` |

### 5.3 照合アルゴリズム

#### パス1: アンカーベースの対応づけ

1. 英語スナップショットと日本語ファイルをそれぞれブロック列に解析する
2. 各ブロックから不変フィンガープリントを抽出する (不変要素を持たないブロックは `None`)
3. 両ファイルのフィンガープリント列に対して **最長共通部分列 (LCS)** を計算する (`difflib.SequenceMatcher` を使用)
4. LCS で一致したペアが、英語ブロックインデックスと日本語ブロックインデックスの対応を確立する

#### パス2: ギャップ内の位置照合

一致したアンカーペア間の「ギャップ」(= アンカーでないブロック群) を、出現順序で 1:1 に対応づける。

```
EN: [anchor_A] [prose_1] [prose_2] [anchor_B]
JA: [anchor_A] [prose_1'] [prose_2'] [anchor_B]
                ↓            ↓
           位置で対応     位置で対応
```

ギャップ内の英語ブロック数と日本語ブロック数が異なる場合は **構造不一致** としてフラグを立てる (→ `validate-structure.py` の守備範囲)。

### 5.4 アンカー密度

このリポジトリのチュートリアルページでは、コードブロックとセクション見出しが平均 5-15 行ごとに出現する。アンカー間のギャップには通常 1-3 個の散文ブロックしか含まれないため、位置照合の信頼性は非常に高い。

インデックスページはコードブロックが少ないが、定義リストの xref パスやセクション見出しがアンカーとして機能する。

---

## 6. マニフェスト JSON スキーマ

### 6.1 ファイル位置

```
tools/translation/manifest.json
```

リポジトリ全体で単一のマニフェストファイルを使用する。Git で追跡する。

### 6.2 スキーマ

```json
{
  "version": 1,
  "upstream_remote": "upstream",
  "upstream_branch": "main",
  "files": {
    "<relative_path>": {
      "upstream_path": "<upstream側の相対パス>",
      "upstream_commit": "<upstream リポジトリの Git コミット SHA — 最後に sync-check.py でチェックした時点の upstream/main HEAD>",
      "initialized_at": "<ISO 8601 タイムスタンプ>",
      "blocks": {
        "<block_id>": {
          "type": "<ブロック種別>",
          "en_hash": "<英語原文のハッシュ (16文字)>",
          "status": "synced | outdated | new | removed",
          "synced_at": "<ISO 8601 タイムスタンプ>"
        }
      }
    }
  }
}
```

### 6.3 ステータス値

| ステータス | 意味 |
|---|---|
| `synced` | 日本語翻訳が現在の英語原文に対応済み |
| `outdated` | upstream で英語が変更されたが、日本語に未反映 |
| `new` | upstream で追加されたブロック (初回翻訳時には存在しなかった) |
| `removed` | upstream でブロックが削除された |

### 6.4 設計上の決定事項

- **日本語のハッシュは保存しない**: 本システムは「upstream の英語変更を検知する」ことが目的であり、日本語側の変更追跡は git で行う。
- **ブロック ID が辞書キー**: O(1) ルックアップを実現する。
- **JSON のキーはソート順で出力**: `json.dump(data, sort_keys=True, indent=2)` により、git diff でのノイズを最小化する。

### 6.5 具体例

```json
{
  "version": 1,
  "upstream_remote": "upstream",
  "upstream_branch": "main",
  "files": {
    "modules/performance/pages/cpu-pinning.adoc": {
      "upstream_path": "modules/performance/pages/cpu-pinning.adoc",
      "upstream_commit": "f764df28a1b2c3d4e5f67890abcdef1234567890",
      "initialized_at": "2026-08-12T10:30:00Z",
      "blocks": {
        "_root/document_header/0": {
          "type": "document_header",
          "en_hash": "a1b2c3d4e5f67890",
          "status": "synced",
          "synced_at": "2026-08-12T10:30:00Z"
        },
        "gaiyou/prose/0": {
          "type": "prose",
          "en_hash": "b2c3d4e5f6789012",
          "status": "outdated",
          "synced_at": "2026-08-12T10:30:00Z"
        },
        "suteppu-1.../code_block/0": {
          "type": "code_block",
          "en_hash": "c3d4e5f678901234",
          "status": "synced",
          "synced_at": "2026-08-12T10:30:00Z"
        }
      }
    }
  }
}
```

---

## 7. スクリプト仕様

全スクリプトは以下の共通規約に従う:

- shebang: `#!/usr/bin/env python3`
- モジュール docstring を日本語で記述
- `argparse` + `RawDescriptionHelpFormatter` + `__doc__` を description に使用
- 位置引数 `paths` (`nargs="*"`, デフォルト `["modules"]`)
- `--dry-run` フラグ (`action="store_true"`)
- ファイル収集: `_collect_files()` (`glob.glob` + `**/*.adoc`)
- 標準ライブラリのみ使用
- 出力形式: `<ファイル>: <サマリー>` + 最終行にトータル集計

### 7.1 `sync-init.py` — スナップショット + マニフェスト初期化

#### 用途

日本語ファイルを翻訳同期管理の対象として登録する。upstream の英語原文を取得し、スナップショットとして保存し、ブロックハッシュを計算してマニフェストに登録する。

#### 使い方

```bash
# 特定ファイルの初期化
python3 tools/translation/sync-init.py modules/networking/pages/linux-bridges.adoc

# ディレクトリ内の全 .adoc を初期化
python3 tools/translation/sync-init.py modules/networking/

# 全モジュールを初期化 (デフォルト)
python3 tools/translation/sync-init.py

# プレビュー
python3 tools/translation/sync-init.py --dry-run
```

#### 引数

| 引数 | 説明 |
|---|---|
| `paths` (位置, `nargs="*"`) | 日本語ファイルまたはディレクトリ。デフォルト: `["modules"]` |
| `--dry-run` | 変更せずに結果を表示 |
| `--force` | 既にマニフェストに存在するファイルも再初期化 |
| `--upstream-ref` | upstream の git ref。デフォルト: `upstream/main` |

#### 処理フロー

1. 指定された日本語 `.adoc` ファイルを収集
2. 各ファイルについて:
   a. upstream パスを決定 (同一相対パス)
   b. `git show <ref>:<path>` で英語原文を取得
   c. upstream にファイルが存在しなければ警告してスキップ
   d. マニフェストに既存かつ `--force` なしなら警告してスキップ
   e. 英語原文を `tools/translation/originals/<相対パス>` にコピー
   f. 英語原文をブロック解析し、各ブロックのハッシュを計算
   g. マニフェストにエントリを作成 (全ブロック `status: "synced"`)
   h. `git rev-parse <ref>` で upstream コミット SHA を記録
3. マニフェストを書き出し

#### 出力例

```
modules/networking/pages/linux-bridges.adoc: initialized (47 blocks)
modules/networking/pages/index.adoc: initialized (23 blocks)

2 files initialized, 70 blocks tracked
```

#### エラー処理

- upstream remote が未設定の場合: `Error: remote 'upstream' not found. Run: git remote add upstream https://github.com/RedHatQuickCourses/ocp-virt-cookbook.git`
- upstream ref が解決できない場合: エラー終了
- `modules/` 配下でないパスが指定された場合: 警告してスキップ

### 7.2 `sync-check.py` — upstream 変更検知

#### 用途

スナップショットと upstream の現在の英語を比較し、変更があったブロックを検知する。変更ブロックを不変アンカー照合で日本語ファイルの行番号にマッピングして報告する。

#### 使い方

```bash
# 全ファイルをチェック
python3 tools/translation/sync-check.py

# 特定のモジュールのみ
python3 tools/translation/sync-check.py modules/networking/

# upstream を先にフェッチしてからチェック
python3 tools/translation/sync-check.py --fetch

# ブロック単位の英語 diff を表示
python3 tools/translation/sync-check.py --verbose
```

#### 引数

| 引数 | 説明 |
|---|---|
| `paths` (位置, `nargs="*"`) | チェック範囲。デフォルト: `["modules"]` |
| `--fetch` | チェック前に `git fetch upstream` を実行 |
| `--verbose` | 変更ブロックの英語 diff を表示 |
| `--dry-run` | マニフェストのステータスを更新しない (レポートのみ) |
| `--upstream-ref` | デフォルト: `upstream/main` |

#### 処理フロー

1. マニフェストを読み込み
2. 指定パスに該当する管理対象ファイルを処理:
   a. スナップショットを `tools/translation/originals/<path>` から読み込み
   b. 現在の upstream 英語を `git show <ref>:<path>` で取得
   c. upstream にファイルが存在しない場合: "upstream で削除" と報告
   d. 両方をブロック解析
   e. ブロック ID でスナップショットと upstream のブロックを照合
   f. 各ブロックの upstream ハッシュを計算し、マニフェストの `en_hash` と比較
   g. ハッシュが異なるブロック → `outdated` にマーク
   h. upstream にあるがマニフェストにないブロック → `new` で追加
   i. マニフェストにあるが upstream にないブロック → `removed` にマーク
3. 変更ブロックについて、不変アンカー照合を実行し日本語ファイルの行番号を特定
4. 管理対象外の upstream ファイル (未翻訳) も別セクションで報告
5. `--dry-run` でなければマニフェストを更新 (ステータスのみ。ハッシュとスナップショットは `sync-mark.py` が更新する)

#### 出力例 (デフォルト)

```
modules/networking/pages/linux-bridges.adoc: 3 outdated, 1 new
  OUTDATED  overview/prose/0                   (ja: L43-46)
  OUTDATED  create-net-attach-def/prose/2      (ja: L125-129)
  OUTDATED  create-net-attach-def/code_block/1 (ja: L148-167)
  NEW       test-secondary-network/prose/3     (ja: mapping not available)

modules/storage/pages/lvm-operator.adoc: synced

1 file with changes, 1 file synced
3 blocks outdated, 1 block new
```

#### 出力例 (--verbose)

上記に加え、各 OUTDATED ブロックに英語 diff を追加:

```
  OUTDATED  overview/prose/0  (ja: L43-46)
    --- snapshot
    +++ upstream
    @@ -1,3 +1,4 @@
     The first step is to define a Linux bridge on the cluster nodes
    -using a `NodeNetworkConfigurationPolicy` (`nncp`).
    +using a `NodeNetworkConfigurationPolicy` (`nncp`). This resource
    +is cluster-scoped, meaning it does not reside in any namespace.
```

#### エラー処理

- マニフェスト未作成: `Error: manifest.json not found. Run sync-init.py first.`
- マニフェストにないファイル: "untracked" セクションに表示

### 7.3 `sync-mark.py` — 翻訳反映済みマーク

#### 用途

翻訳者が日本語テキストを更新した後、該当ブロックを「反映済み」としてマークする。マニフェストのハッシュとスナップショットを現在の upstream に更新する。

#### 使い方

```bash
# ファイル内の全 outdated/new ブロックをマーク
python3 tools/translation/sync-mark.py modules/networking/pages/linux-bridges.adoc

# 特定ブロックのみマーク
python3 tools/translation/sync-mark.py modules/networking/pages/linux-bridges.adoc \
  --blocks "overview/prose/0" "create-net-attach-def/prose/2"

# 全管理対象ファイルの全ブロックをマーク
python3 tools/translation/sync-mark.py --all

# プレビュー
python3 tools/translation/sync-mark.py --dry-run
```

#### 引数

| 引数 | 説明 |
|---|---|
| `paths` (位置, `nargs="*"`) | 対象ファイル。デフォルト: `["modules"]` |
| `--blocks` | マークする特定のブロック ID リスト (省略時は全 outdated/new ブロック) |
| `--all` | 全管理対象ファイルを対象にする |
| `--dry-run` | 変更せずに結果を表示 |

#### 処理フロー

1. 各対象ファイルについて:
   a. 現在の upstream 英語を `git show upstream/main:<path>` で取得
   b. ブロック解析し、新しいハッシュを計算
   c. ファイル内の全 `outdated` / `new` ブロックについて:
      - `en_hash` を新しいハッシュに更新
      - `synced_at` を現在時刻に更新
      - `status` を `synced` に設定
      - `removed` ステータスのブロックはマニフェストから削除
   d. 全ブロックが `synced` であることを確認し、スナップショットファイルを現在の upstream 内容で上書き
   e. `upstream_commit` を現在の `upstream/main` HEAD に更新
2. マニフェストを書き出し

#### 出力例

```
modules/networking/pages/linux-bridges.adoc: 3 blocks marked synced
  SYNCED  overview/prose/0
  SYNCED  create-net-attach-def/prose/2
  SYNCED  create-net-attach-def/code_block/1

1 file updated, 3 blocks synced
```

### 7.4 `sync-translate.py` — AI による自動翻訳

#### 用途

`sync-check.py` で検知された `outdated` / `new` ブロックを Gemini API で自動翻訳し、日本語ファイルに直接適用する。翻訳作業は main ブランチから作成した新規ローカルブランチ上で行い、人間がレビューできる状態で停止する（`git add` / `git commit` / `git push` は行わない）。

#### 使い方

```bash
# 全管理対象ファイルの outdated/new ブロックを翻訳
python3 tools/translation/sync-translate.py

# モデルを指定
python3 tools/translation/sync-translate.py --model gemini-2.5-pro

# dry-run (翻訳結果を表示するが適用しない、ブランチも作成しない)
python3 tools/translation/sync-translate.py --dry-run

# ブランチ名を指定
python3 tools/translation/sync-translate.py --branch translate/2026-08-12

# 中断した翻訳を再開
python3 tools/translation/sync-translate.py --resume
```

#### 引数

| 引数 | 説明 |
|---|---|
| `--dry-run` | 翻訳結果を stdout に表示するが、ファイルへの書き込み・ブランチ作成・`sync-mark.py` の実行を行わない |
| `--model` | Gemini モデル。デフォルト: `gemini-3.7-flash` |
| `--branch` | 作成するブランチ名。デフォルト: `translate/<YYYY-MM-DD>` (実行日) |
| `--resume` | 中断した翻訳を再開する。既存の translate ブランチに切り替え、プログレスファイルから翻訳済みファイルをスキップして続行する |

#### 前提条件

- Python 3.9 以上 (標準ライブラリのみ使用、外部パッケージ不要)
- `GEMINI_API_KEY` 環境変数が設定されていること
- 作業ディレクトリがクリーンであること (`git status` で未コミットの変更がないこと)

##### Gemini API キーの設定

1. [Google AI Studio](https://aistudio.google.com/apikey) にアクセスし、API キーを発行する
2. 環境変数に設定する:
   ```bash
   export GEMINI_API_KEY="your-api-key-here"
   ```
3. 永続化する場合はシェルの設定ファイル (`~/.zshrc`, `~/.bashrc` 等) に追記する

#### 処理フロー

1. `--resume` でない場合: 作業ディレクトリがクリーンであることを確認 (未コミットの変更があればエラー終了)。`--resume` の場合: 既存の translate ブランチに切り替え、プログレスファイル (`tools/translation/.translate-progress.json`) を読み込む
2. `--resume` でない場合: 現在のブランチから新規ローカルブランチを作成し切り替え (`git switch -c <branch>`)。プログレスファイルを初期化する
3. `sync-check.py` を内部的に呼び出し、マニフェストのステータスを最新の upstream と照合して更新する (`git fetch upstream` も自動実行)。マニフェストの更新は新ブランチ上で行われるため、翻訳結果と共にレビュー対象となる
3a. プログレスファイルに `sync_check_done: true` を記録する。`--resume` 時に `sync_check_done` が true であれば sync-check をスキップする
4. **upstream 新規ファイル・モジュールの検出と自動登録**: upstream に存在するが日本語リポジトリに存在しない `.adoc` ファイルおよびモジュールディレクトリを検出する。新規モジュールの場合はディレクトリ構造の作成、`nav.adoc` のコピー・翻訳、`antora.yml` への追加を行う (9.15 参照)。各ページファイルは `sync-init.py` を内部的に呼び出して管理対象に登録し、全ブロックを AI API で翻訳して日本語ファイルとして新規作成する (後述の「新規ファイル翻訳」参照)。`--resume` 時はプログレスファイルの `completed_files` に含まれるファイルをスキップする。各ファイルの翻訳完了後にプログレスファイルを更新する。対応する `attachments/` および `images/` も upstream からコピーする
5. **upstream で削除されたファイル・モジュールの削除**: upstream に対応するファイルが存在しない管理対象ファイルを検出し、日本語ファイル、マニフェストエントリ、スナップショットを削除する (9.4 参照)。モジュール全体が削除された場合はディレクトリごと削除し `antora.yml` からも除去する (9.15 参照)。upstream に一度も存在しなかった日本語独自ファイルも同様に削除する (9.12 参照)
6. マニフェストを読み込み、`outdated` / `new` / `removed` ブロックを持つファイルを特定
7. 各ファイルについて。`--resume` 時は `completed_files` に含まれるファイルをスキップする:
   a. スナップショット英語 (`originals/<path>`) を読み込み
   b. 現在の upstream 英語を `git show upstream/main:<path>` で取得
   c. 現在の日本語ファイルを読み込み
   d. 不変アンカー照合で英語スナップショットと日本語ブロックを対応づけ
   d2. 構造不一致の自動修正: スナップショット英語に存在するが日本語に対応ブロックがないもの (AI 翻訳時の欠損) はスナップショットを参考に翻訳して挿入する。逆に日本語にあるがスナップショットに対応しないブロック (AI 翻訳時の余剰) は削除する。修正後、再度アンカー照合を行い 1:1 対応を確立する
   e. コードブロック翻訳: `code_block` は upstream 原文をベースに AI API でコメント行 (`#` で始まる行) のみ翻訳する (コード本体は不変)。`literal_block` および `block_attribute` は upstream 原文をそのまま使用する (AI API は呼び出さない)
   f. `outdated` ブロック (上記以外): Gemini API で翻訳を生成し、日本語ファイル内の該当ブロックを置換 (後述のプロンプト設計参照)
   g. `new` ブロック (上記以外): AI API で翻訳を生成し、日本語ファイル内の適切な位置に挿入
   h. `removed` ブロック: 日本語ファイルから該当ブロックを削除
   i. 画像ファイルの同期: upstream のページが参照する画像 (`image::`) のうち、日本語リポジトリに存在しないものを `git show upstream/main:<path>` で取得してコピーする。upstream で削除された画像 (日本語側に存在するが upstream に存在しない) は削除する
   j. attachments/images の更新同期: 処理対象ファイルが属するモジュールの `attachments/` および `images/` 配下の既存ファイルを upstream と比較し、内容が異なるファイルを upstream で上書きする (9.11 参照)
   k. 日本語ファイルを書き出し
8. **nav.adoc の同期**: 各モジュールの `nav.adoc` を upstream と比較し、差分を反映する。新規ページへの xref 行を追加し、削除されたページの xref 行を削除する。xref の表示テキスト (`[]` 内) がある場合は AI API で翻訳する
9. **antora.yml の同期**: upstream の `antora.yml` と比較し、`nav` セクションに新規モジュールの追加・削除を反映する。`name` フィールドなどリポジトリ固有の値は変更しない
9a. **antora-playbook.yml の同期**: upstream の `antora-playbook.yml` をベースに、JA 固有フィールド (`site.start_page` のコンポーネント名、`asciidoc.attributes.build-date`) のみ上書き保持して同期する (9.14.1 参照)
10. 書式整形ツール (`add-jp-lat-spaces.py`, `convert-fullwidth-parens.py`) を実行
10a. 既存翻訳の事後補正 (アドモニションキーワード復元、見出し用語集適用) を全ファイルに対して実行 (9.20 参照)
11. `sync-mark.py` を内部的に呼び出してマニフェストとスナップショットを更新 (`removed` ブロックはマニフェストから除去)
11a. プログレスファイルを削除する (正常完了時のみ)
12. 完了メッセージを表示 (レビュー手順のガイドを含む)

**注意**: ステップ 12 の後、`git add` / `git commit` / `git push` は一切行わない。翻訳結果はワーキングツリー上の未ステージ変更として残る。翻訳者が `git diff` でレビューした後、手動でコミット・プッシュする。

#### プロンプト設計

##### `outdated` ブロック用プロンプト

```
あなたは技術文書の翻訳者です。OpenShift Virtualization に関する英語ドキュメントの日本語翻訳を更新してください。

## ルール
- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、リンク等) はそのまま維持すること
- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は英語のまま残すこと
- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。日本語に翻訳しないこと
- インラインアドモニション (NOTE: テキスト) とブロックアドモニション ([NOTE]\n====) の形式を相互に変換しないこと
- 既存の日本語翻訳のスタイルと文体を維持すること
- 翻訳文のみを出力し、説明や注記は付けないこと

## セクション: {section_title}

## 旧英語 (翻訳元):
{old_english_block}

## 新英語 (更新後):
{new_english_block}

## 現在の日本語翻訳:
{current_japanese_block}

## 指示:
旧英語から新英語への変更点を、現在の日本語翻訳に反映してください。変更がない部分はそのまま維持してください。
```

##### `new` ブロック用プロンプト

```
あなたは技術文書の翻訳者です。OpenShift Virtualization に関する英語ドキュメントを日本語に翻訳してください。

## ルール
- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、リンク等) はそのまま維持すること
- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は英語のまま残すこと
- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。日本語に翻訳しないこと
- インラインアドモニション (NOTE: テキスト) とブロックアドモニション ([NOTE]\n====) の形式を相互に変換しないこと
- 以下の前後のブロックの文体に合わせること
- 翻訳文のみを出力し、説明や注記は付けないこと

## セクション: {section_title}

## 前のブロック (文体参考):
{previous_japanese_block}

## 翻訳対象の英語:
{new_english_block}

## 後のブロック (文体参考):
{next_japanese_block}
```

##### コードブロックの扱い

コードブロック (`code_block`) のコメント行 (`#` で始まる行) は翻訳対象とし、コード本体は不変とする。`literal_block` および `block_attribute` は完全に不変 (翻訳しない)。以下のルールで処理する:

- **コードブロック (`code_block`)**: AI API でコメント行のみ翻訳する。コード本体 (コマンド、YAML キー・値、変数等) は変更しない。`outdated` / `new` / `synced` のいずれのステータスでも、upstream の英語原文をベースに AI API でコメント翻訳を行う。日本語ファイル内に既にコメントが翻訳済みの場合は、現在の日本語訳を参考にして更新する。
- **リテラルブロック (`literal_block`)**: upstream の英語原文をそのまま使用する。AI API は呼び出さない。
- **block_attribute**: upstream の英語原文をそのまま使用する。

コードブロック翻訳用プロンプトでは、コメント行 (`#` で始まる行) のみを翻訳し、それ以外の行は一切変更しないことを明示的に指示する。

##### 新規ファイル翻訳 (処理フロー ステップ 4)

upstream に存在するが日本語リポジトリに存在しないファイルは、ファイル全体を翻訳して新規作成する。処理手順:

1. `sync-init.py` を内部的に呼び出し、スナップショットとマニフェストに登録
2. upstream 英語ファイルをブロック解析
3. 各ブロックを種別に応じて処理:
   - コードブロック (`code_block`): AI API でコメント行のみ翻訳 (コード本体は不変)
   - リテラルブロック / block_attribute: 英語原文をそのままコピー
   - セクション見出し / 散文 / リスト / テーブル等: AI API で翻訳
4. 翻訳結果を日本語ファイルとして `modules/<module>/pages/` に書き出し
5. 対応する `attachments/` および `images/` 配下のファイルも upstream からコピー

```
あなたは技術文書の翻訳者です。OpenShift Virtualization に関する英語ドキュメントを日本語に翻訳してください。

## ルール
- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、リンク等) はそのまま維持すること
- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は英語のまま残すこと
- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。日本語に翻訳しないこと
- インラインアドモニション (NOTE: テキスト) とブロックアドモニション ([NOTE]\n====) の形式を相互に変換しないこと
- 翻訳文のみを出力し、説明や注記は付けないこと
- 自然な日本語で、技術的に正確な翻訳を行うこと

## 翻訳対象の英語:
{english_block}
```

#### 出力例

```
CHECKED     sync-check.py executed (fetched upstream, 1 file with changes)
BRANCH      Created branch 'translate/2026-08-13' from main

NEW FILE    modules/agentic-vm-management/pages/index.adoc (32 blocks translated)
NEW FILE    modules/agentic-vm-management/pages/getting-started.adoc (48 blocks translated)
  ...
COPIED      modules/agentic-vm-management/attachments/getting-started/mcps.json
DELETED     modules/LABENV/pages/index.adoc (no upstream counterpart)

modules/networking/pages/linux-bridges.adoc:
  TRANSLATED  overview/prose/0  (outdated → synced)
  TRANSLATED  create-net-attach-def/prose/2  (outdated → synced)
  TRANSLATED  create-net-attach-def/code_block/1  (code block comments translated)
  TRANSLATED  test-secondary-network/prose/3  (new → synced)
  REMOVED     deprecated-section/prose/0  (removed from upstream)

IMAGE SYNC  modules/networking/images/layer2-secondary-topology.png: copied from upstream
IMAGE SYNC  modules/performance/images/performance-tuning-ladder.svg: copied from upstream
  ...
ASSET SYNC  modules/networking/attachments/linux-bridges/bridge-demo-vm.yaml: updated from upstream
ASSET SYNC  modules/networking/attachments/linux-bridges/linux-bridge-nad.yaml: updated from upstream
  ... (17 attachment files updated)
NAV SYNC    modules/getting-started/nav.adoc: +4 xref lines added
NAV SYNC    modules/networking/nav.adoc: +3 xref lines added
NAV SYNC    modules/agentic-vm-management/nav.adoc: created (new module)
ANTORA      antora.yml: +1 module added (agentic-vm-management)
FORMATTED   add-jp-lat-spaces.py applied
FORMATTED   convert-fullwidth-parens.py applied
MARKED      sync-mark.py executed

24 new files translated, 1 file updated, 3 blocks translated, 1 code block comments translated, 1 block removed, 16 images copied, 17 attachments updated

Review your changes:
  git diff
  git diff --stat
When satisfied, commit and push manually.
```

#### 出力例 (--resume)

```
RESUME      Resuming on branch 'translate/2026-08-13' (5 files already completed)
SKIP        modules/agentic-vm-management/pages/ai-skill-chaining.adoc (already translated)
SKIP        modules/agentic-vm-management/pages/ai-vm-lifecycle.adoc (already translated)
  ...
  TRANSLATING ai-vm-rebalance.adoc (134 blocks)..........
NEW FILE    modules/agentic-vm-management/pages/ai-vm-rebalance.adoc (134 blocks translated)
  ...
```

#### エラー処理

- 作業ディレクトリがクリーンでない: `Error: Working directory has uncommitted changes. Commit or stash them first.`
- ブランチが既に存在 (`--resume` なし): `Error: Branch 'translate/2026-08-12' already exists. Use --resume to continue, or --branch to specify a different name.`
- `--resume` 時にブランチが存在しない: `Error: Branch 'translate/2026-08-13' not found. Run without --resume to start a new translation.`
- `--resume` 時にプログレスファイルが存在しない: `Error: Progress file not found. The previous run may have completed successfully.`
- API キー未設定: `Error: GEMINI_API_KEY environment variable not set`
- API レート制限 (429): `Retry-After` ヘッダーまたは指数バックオフで待機。3 回連続時はバッチサイズを半減して再試行 (9.21.3 参照)
- API エラー: 該当ブロックをスキップし、スキップされたブロックの一覧を最後に表示。スキップがあった場合は `sync-mark.py` を実行しない
- マニフェスト未作成: `Error: manifest.json not found. Run sync-init.py first.`
- 変更対象ブロックなし: `No outdated or new blocks found. Nothing to translate.`

### 7.5 `sync-status.py` — 翻訳カバレッジダッシュボード

#### 用途

翻訳状況の全体ダッシュボードを表示する。管理対象ファイルのステータスサマリー、未翻訳ファイルの一覧を出力する。

#### 使い方

```bash
# フルダッシュボード
python3 tools/translation/sync-status.py

# サマリーのみ
python3 tools/translation/sync-status.py --summary
```

#### 引数

| 引数 | 説明 |
|---|---|
| `paths` (位置, `nargs="*"`) | 範囲。デフォルト: `["modules"]` |
| `--summary` | ファイル単位の集計のみ表示 (ブロック詳細なし) |
| `--upstream-ref` | デフォルト: `upstream/main` |

#### 処理フロー

1. マニフェストを読み込み
2. `git ls-tree -r --name-only <ref> modules/` で upstream の全 `.adoc` ページファイルを列挙
3. 各ファイルを分類:
   - **管理対象 & 全同期済み**: マニフェストに存在し、全ブロックが `synced`
   - **管理対象 & 変更あり**: マニフェストに存在し、`outdated` または `new` のブロックあり
   - **未管理**: マニフェストに存在しない (未翻訳)
4. 集計を出力

#### 出力例

```
=== Translation Status ===

管理対象ファイル:      53 / 77 upstream files (68.8%)
全同期済み:           47
変更あり:              6
  modules/networking/pages/linux-bridges.adoc          3 outdated, 1 new
  modules/storage/pages/lvm-operator.adoc              1 outdated
  modules/vm-lifecycle/pages/cloning-vms.adoc          2 outdated
  modules/performance/pages/cpu-pinning.adoc           1 outdated
  modules/getting-started/pages/prerequisites.adoc     5 outdated
  modules/api/pages/api-component-overview.adoc        1 new

未翻訳 upstream ファイル:  24
  modules/agentic-vm-management/pages/index.adoc
  modules/agentic-vm-management/pages/getting-started.adoc
  ...

Total blocks: 2,847 synced / 2,860 tracked (99.5%)
```

### 7.6 `validate-structure.py` — 構造一致バリデーション

#### 用途

英語スナップショットと日本語ファイルのブロック構造が 1:1 で対応していることを検証する。運用ルール (段落の統合・分割・並べ替え禁止) の違反を検出する。

#### 使い方

```bash
# 全管理対象ファイルを検証
python3 tools/translation/validate-structure.py

# 特定ファイルのみ
python3 tools/translation/validate-structure.py modules/networking/pages/linux-bridges.adoc

# CI用: 違反があれば非ゼロ終了
python3 tools/translation/validate-structure.py --strict
```

#### 引数

| 引数 | 説明 |
|---|---|
| `paths` (位置, `nargs="*"`) | 範囲。デフォルト: `["modules"]` |
| `--strict` | 違反があれば終了コード 1 で終了 (CI 用) |

#### 処理フロー

1. 各管理対象ファイルについて:
   a. スナップショット英語を `tools/translation/originals/<path>` から読み込み
   b. 日本語ファイルを `modules/` から読み込み
   c. 両方をブロック解析
   d. セクションごとにブロック種別の数を比較:
      - 一致すれば OK
      - 不一致なら WARNING (どのセクションで何が何個ずれているか)
   e. セクション見出しの順序と階層レベルが一致するか検証
   f. コードブロックの内容が同一か検証 (コードは翻訳しないため)
2. 結果を出力

#### 出力例

```
modules/networking/pages/linux-bridges.adoc: OK (47 blocks)

modules/storage/pages/lvm-operator.adoc: 2 violations
  WARNING  section "prerequisites": EN has 3 prose, JA has 4
           (ja: L15-28 -- paragraph may have been split)
  WARNING  section "step-2": code_block/1 content differs
           (ja: L89 -- code block may have been modified)

1 file with violations, 1 file OK
```

---

## 8. ディレクトリ構成

```
tools/translation/
  README.md                          # 既存 — 同期ツールの使い方を追記
  SYNC-SPEC.md                       # 本仕様書
  add-jp-lat-spaces.py               # 既存
  convert-fullwidth-parens.py         # 既存
  sync-init.py                       # 新規
  sync-check.py                      # 新規
  sync-translate.py                  # 新規 (要 google-genai パッケージ)
  sync-mark.py                       # 新規
  sync-status.py                     # 新規
  validate-structure.py              # 新規
  manifest.json                      # 新規 (自動生成、Git 管理)
  .translate-progress.json           # 翻訳進捗 (実行中のみ存在、完了時に削除)
  _lib/                              # 新規 — 共有ライブラリ
    __init__.py
    block_parser.py                  # ブロック解析 (セクション2)
    manifest.py                      # マニフェスト読み書き (セクション6)
    anchor_matching.py               # 不変アンカー照合 (セクション5)
    git_utils.py                     # Git 操作
    common.py                        # _collect_files(), 出力ヘルパー
  originals/                         # 新規 — スナップショットディレクトリ (Git 管理)
    modules/
      networking/pages/*.adoc        # 英語原文のコピー
      storage/pages/*.adoc
      performance/pages/*.adoc
      ...
```

### 8.1 Git 管理対象

| ファイル/ディレクトリ | Git 管理 | 理由 |
|---|---|---|
| `manifest.json` | 対象 | 翻訳状況の唯一の情報源。変更履歴が意味を持つ |
| `originals/` | 対象 | diff の基準。upstream remote なしでもツールが動作可能にする |
| `_lib/` | 対象 | 共有ライブラリ |
| 5つのスクリプト | 対象 | ツール本体 |
| `.translate-progress.json` | 対象外 | 実行中の一時ファイル。translate ブランチ上にのみ存在し、正常完了時に削除される |

---

## 9. エッジケース

### 9.1 upstream でセクションが追加された場合

- `sync-check.py` が新しいブロック ID を `new` ステータスで検出
- 翻訳者はセクションを翻訳して追加した後、`sync-mark.py` でマーク
- 既存セクション内のブロック ordinal には影響しない (セクションスコープで独立)

### 9.2 upstream でセクションが削除された場合

- `sync-check.py` がマニフェスト上のブロックを `removed` ステータスにマーク
- `sync-translate.py` が日本語ファイルから該当ブロックを自動的に削除する
- `sync-mark.py` (内部実行) により、`removed` ブロックがマニフェストから除去される

### 9.3 upstream でファイルが追加された場合

- `sync-translate.py` が自動的に検出し、以下を実行する:
  1. `sync-init.py` を内部的に呼び出してスナップショットとマニフェストに登録
  2. 全ブロックを AI API で翻訳し、日本語ファイルとして新規作成
  3. 対応する `attachments/` および `images/` 配下のファイルも upstream からコピー
  4. 該当モジュールの `nav.adoc` に xref 行を追加 (upstream の nav.adoc に合わせる)
- 以降は既存ファイルと同様に管理対象として追跡される
- 新規モジュールの追加は 9.15 を参照

### 9.4 upstream でファイルが削除された場合

- `sync-translate.py` が自動的に検出し、以下を実行する:
  1. 日本語ファイルを削除
  2. マニフェストから該当ファイルのエントリを除去
  3. スナップショット (`originals/` 配下) の対応ファイルを削除
  4. 該当モジュールの `nav.adoc` から対応する xref 行を削除
  5. 対応する `attachments/` および `images/` のうち、他のページから参照されていないファイルを削除
- モジュール全体の削除は 9.15 を参照

### 9.5 upstream でファイルがリネームされた場合

- ツールからは「旧ファイル削除 (9.4) + 新ファイル追加 (9.3)」として自動処理される
- 旧ファイルの翻訳は再利用されない (新ファイルとして全ブロックを新規翻訳する)

### 9.6 upstream でセクション見出しが変更された場合

- ブロック ID が変更される (セクションパスが変わるため)
- `sync-check.py` では旧 ID のブロックが `removed`、新 ID のブロックが `new` として報告される
- `sync-translate.py` は以下の順序で自動処理する:
  1. `removed` と `new` のペアを不変アンカー照合で突き合わせる。旧セクション配下のコードブロックや block_attribute などの不変要素が新セクション配下と一致する場合、**セクション見出しのリネーム** と判定する
  2. リネームと判定された場合: セクション見出しのみ AI API で翻訳し、配下のブロックは既存の日本語翻訳をそのまま維持する (無駄な再翻訳を回避)
  3. リネームと判定されなかった場合 (配下ブロック構造が大きく異なる): `removed` ブロックを削除し、`new` ブロックを通常通り新規翻訳する

**具体例**: `getting-started/index.adoc` で `== What You'll Learn` → `== What You Will Learn` + `== Learning Path` が追加されたケース。`What You'll Learn` 配下のブロックが `What You Will Learn` 配下と一致すれば、見出しのみ翻訳し配下は流用する。`Learning Path` は新セクションとして新規翻訳する。

### 9.7 コードブロックが upstream で変更された場合

- コードブロックのコード本体は不変だが、コメント行 (`#` で始まる行) は翻訳対象
- `sync-check.py` がハッシュ変更を検知し `outdated` として報告
- `sync-translate.py` が upstream のコードブロックをベースに、AI API でコメント行のみ翻訳する
- 日本語ファイル内に既にコメントが翻訳済みの場合は、現在の日本語訳を参考にして差分を更新する
- `validate-structure.py` もコードブロック内容の不一致を検出する

### 9.8 部分更新 (ファイル内の一部ブロックのみ翻訳反映)

- 運用ルール (セクション 1.3 ルール 4) により、部分更新は行わない
- `sync-check.py` で変更が検知されたファイルは、全 `outdated` / `new` ブロックを翻訳に反映した上で `sync-mark.py` を実行する
- これにより `sync-mark.py` は常にファイル内の全ブロックを `synced` に更新し、スナップショットも最新の upstream に一括更新する
- `sync-mark.py --blocks` オプションは残すが、運用上は使用しない (デバッグ・例外対応用)

### 9.9 マニフェスト未作成状態での各スクリプト実行

- `sync-init.py`: マニフェストを新規作成 (`version: 1`, `files: {}`)
- `sync-check.py`: エラー終了 (`manifest.json not found`)
- `sync-mark.py`: エラー終了 (`manifest.json not found`)
- `sync-status.py`: マニフェストなしでも動作し、全ファイルを "未翻訳" として表示
- `validate-structure.py`: マニフェストなしの場合はスナップショットも存在しないためエラー終了

### 9.10 `nav.adoc` ファイル

- ナビゲーションファイルはリスト項目 (xref) のみで構成される単純な構造
- 通常のページファイルと同様に管理対象にできる
- ブロック種別は `list_item` が主体

### 9.11 `attachments/` および `images/` 配下のファイル

これらのファイルは翻訳対象ではなく、upstream と同一であるべき。`sync-translate.py` が以下のタイミングで自動的に同期する:

- **新規ファイル翻訳時 (ステップ 4)**: 新規ページに対応する `attachments/` および `images/` を upstream からコピー
- **既存ファイル処理時 (ステップ 7i)**: upstream の変更でページに `image::` 参照が追加された場合、参照先の画像ファイルを upstream からコピー。upstream で削除された画像は日本語側からも削除
- **既存 attachments/images の更新同期 (ステップ 7j)**: 管理対象モジュール内の既存 `attachments/` および `images/` ファイルを upstream と比較し、内容が異なるファイルを upstream の内容で上書きする。upstream で削除されたファイルは日本語側からも削除する
- **モジュール削除時 (ステップ 5)**: モジュールディレクトリごと削除する際に `attachments/` および `images/` も含めて削除

### 9.12 日本語リポジトリ独自ファイル

upstream に対応するファイルが存在しない日本語ファイルは、翻訳対象ではないため削除する。

- `sync-translate.py` 実行時 (処理フロー ステップ 5) に、日本語リポジトリの `modules/` 配下にある `.adoc` ファイルのうち、upstream に対応するファイルが存在しないものを検出する
- 該当ファイルを日本語リポジトリから削除する
- マニフェストにエントリがあれば除去する
- 該当ファイルを参照している `nav.adoc` の xref 行も削除する
- upstream に存在しないモジュール全体 (例: `modules/LABENV/`) は、`pages/`、`nav.adoc`、`images/`、`attachments/` を含むモジュールディレクトリごと削除し、`antora.yml` の `nav` セクションからも除去する
- 同一モジュール内に upstream に存在するファイルと存在しないファイルが混在する場合 (例: `modules/appendix/` — `glossary.adoc` は upstream に存在するが `appendix.adoc` は存在しない) は、独自ファイルのみ削除し、モジュール自体は残す
- 出力例: `DELETED  modules/LABENV/pages/index.adoc (no upstream counterpart)`

**注意**: 独自ファイルの削除はブランチ上で行われるため、レビュー時に確認できる。意図的に残したいファイルがある場合は、レビュー時に `git checkout` で復元する。

### 9.13 nav.adoc の同期

`nav.adoc` は upstream と日本語リポジトリ間で xref 行の構成を一致させる必要がある。

- `sync-translate.py` 実行時 (処理フロー ステップ 8) に、各モジュールの `nav.adoc` を upstream と比較する
- upstream で追加された xref 行は、日本語側の `nav.adoc` にも追加する。`[]` 内の表示テキストがある場合は AI API で翻訳する
- upstream で削除された xref 行は、日本語側からも削除する
- upstream で新規モジュールが追加された場合、そのモジュールの `nav.adoc` を upstream からコピーし、表示テキストを翻訳する
- `nav.adoc` 自体もマニフェストで管理対象にできるが、xref 行の追加削除は構造変更に該当するため、ステータス管理ではなく upstream との直接比較で同期する

### 9.14 antora.yml の同期

`antora.yml` はサイト構成ファイルであり、upstream でモジュールが追加・削除された際に同期が必要。

- `sync-translate.py` 実行時 (処理フロー ステップ 9) に、upstream の `antora.yml` の `nav` セクションと比較する
- upstream で追加されたモジュールの nav エントリを日本語側にも追加する
- upstream で削除されたモジュールの nav エントリを日本語側からも削除する
- `name` フィールドはリポジトリ固有の値 (`ocp-virt-cookbook_ja`) であり、upstream と異なる値を維持する
- `title`, `version` 等は upstream に合わせて更新する

### 9.14.1 antora-playbook.yml の同期

`antora-playbook.yml` はサイトレベルの構成ファイルであり、ブロック単位の翻訳管理 (マニフェスト) の範囲外とする。ただし、upstream で追加・変更された設定 (拡張機能、UI、AsciiDoc 属性等) を日本語リポジトリに反映するため、`sync-translate.py` 実行時 (処理フロー ステップ 9a) に同期を行う。

#### 同期方針

upstream の `antora-playbook.yml` をベースとし、**JA 固有フィールドのみ上書き保持** する。

#### JA 固有フィールド

| フィールド | JA での値 | 理由 |
|---|---|---|
| `site.start_page` | `ocp-virt-cookbook_ja::index.adoc` | `antora.yml` の `name` が `ocp-virt-cookbook_ja` であるため、コンポーネント名部分を一致させる必要がある |
| `asciidoc.attributes.build-date` | `'@'` | PDF 生成で使用する JA 独自属性 |

上記以外のフィールドは upstream の値をそのまま採用する。

#### 処理手順

1. upstream の `antora-playbook.yml` を `git show upstream/main:antora-playbook.yml` で取得
2. YAML としてパースする (PyYAML は標準ライブラリではないため、行単位の文字列処理で対応する)
3. `site.start_page` のコンポーネント名部分を JA の `antora.yml` の `name` フィールド値に置換する
   - パターン: `<upstream_component_name>::<page>` → `<ja_component_name>::<page>`
   - 例: `ocp-virt-cookbook::index.adoc` → `ocp-virt-cookbook_ja::index.adoc`
4. JA 独自の `asciidoc.attributes` をマージする (upstream に存在しない属性を追加、upstream に存在する属性は upstream の値を優先)
5. 結果を `antora-playbook.yml` に書き出す

#### 具体例

upstream:
```yaml
site:
  title: OpenShift Virtualization cookbook
  start_page: ocp-virt-cookbook::index.adoc

antora:
  extensions:
    - require: '@antora/lunr-extension'
      index_latest_only: true

content:
  sources:
  - url: ./

asciidoc:
  attributes:
    page-pagination: true

ui:
  bundle:
    url: ./ui-bundle/ui-bundle.zip
  supplemental_files: ./supplemental-ui
```

同期後の JA:
```yaml
site:
  title: OpenShift Virtualization cookbook
  start_page: ocp-virt-cookbook_ja::index.adoc

antora:
  extensions:
    - require: '@antora/lunr-extension'
      index_latest_only: true

content:
  sources:
  - url: ./

asciidoc:
  attributes:
    page-pagination: true
    build-date: '@'

ui:
  bundle:
    url: ./ui-bundle/ui-bundle.zip
  supplemental_files: ./supplemental-ui
```

#### 変更検知

- upstream と JA の `antora-playbook.yml` を比較し、差分がある場合のみ書き出す
- 変更があった場合は `PLAYBOOK  antora-playbook.yml: updated from upstream` と出力する
- 変更がない場合は出力しない

### 9.15 モジュール (ディレクトリ) 単位の追加・削除・リネーム

ファイル単位の操作 (9.3-9.5) はモジュール内の個別ファイルを対象とするが、モジュールディレクトリ自体の追加・削除・リネームはより広い範囲に影響する。`sync-translate.py` は以下を自動処理する。

#### モジュール追加

upstream に存在するが日本語リポジトリに存在しないモジュールディレクトリを検出した場合:

1. モジュールディレクトリ構造を作成 (`pages/`, `attachments/`, `images/`)
2. `nav.adoc` を upstream からコピーし、表示テキスト (`[]` 内) を AI API で翻訳
3. 全ページファイルを 9.3 の手順で翻訳・作成
4. `attachments/` および `images/` 配下のファイルを upstream からコピー
5. `antora.yml` の `nav` セクションに該当モジュールのエントリを追加 (9.14 参照)

**具体例**: `modules/agentic-vm-management/` — upstream で新設されたモジュール。8つのページファイル、`nav.adoc`、`attachments/getting-started/mcps.json` をまとめて作成する。

#### モジュール削除

upstream から削除されたモジュール (日本語側に存在するが upstream に存在しないモジュール) を検出した場合:

1. モジュールディレクトリを `pages/`、`nav.adoc`、`attachments/`、`images/` を含めてまるごと削除
2. マニフェストから該当モジュール内の全ファイルのエントリを除去
3. スナップショット (`originals/` 配下) の該当モジュールディレクトリを削除
4. `antora.yml` の `nav` セクションから該当モジュールのエントリを削除 (9.14 参照)

**注意**: 9.12 (日本語リポジトリ独自ファイル) との違い — 9.12 は「upstream に一度も存在しなかったファイル/モジュール」を対象とし、9.15 は「以前 upstream に存在したが削除されたモジュール」を対象とする。処理としては同一 (ディレクトリ削除 + antora.yml 更新) だが、起源が異なる。

#### モジュールリネーム

- ツールからは「旧モジュール削除 + 新モジュール追加」として見える
- 旧モジュールの翻訳は再利用されない (新モジュールとして全ファイルを新規翻訳する)

### 9.16 翻訳の中断と再開

翻訳実行中にプロセスが中断された場合 (API タイムアウト、手動 kill、Ctrl+C 等):

- 翻訳済みファイルは translate ブランチ上のディスクに残る
- プログレスファイル (`tools/translation/.translate-progress.json`) に翻訳済みファイルのリストが記録されている
- `--resume` フラグで再開すると、翻訳済みファイルをスキップして続行する
- プログレスの粒度はファイル単位。翻訳途中のファイル (全ブロック完了前に中断) は最初から再翻訳する
- プログレスファイルは正常完了時に自動削除される
- 完全にやり直したい場合は translate ブランチを削除してから `--resume` なしで実行する

### 9.17 アドモニションキーワードの保護

AsciiDoc のインラインアドモニション (`NOTE:`, `WARNING:`, `IMPORTANT:`, `TIP:`, `CAUTION:`) のキーワード部分は英語でなければ正しく描画されない。AI 翻訳時にキーワードが日本語に変換される問題への対策:

#### プロンプトによる抑止

全翻訳プロンプト (outdated / new / new-file) に以下のルールを含める:

- 「AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。日本語に翻訳しないこと」
- 「インラインアドモニション (NOTE: テキスト) とブロックアドモニション ([NOTE]\n====) の形式を相互に変換しないこと」

#### プレフィックス分離による保護

`admonition_inline` ブロックの翻訳時は、`list_item` と同様にプレフィックス分離方式を適用する:

1. 翻訳前: キーワードプレフィックス (`NOTE: ` 等) を分離し、本文のみを AI に送信
2. 翻訳後: 元のキーワードプレフィックスを再付与

#### ポストプロセス検証

全翻訳パス (outdated / new / structure-fix / new-file) で翻訳結果に対して以下を検証する:

- 翻訳結果の先頭が日本語のアドモニションキーワード (`注:`, `重要:`, `ヒント:`, `警告:`, `注意:`, `注記:`) で始まる場合、対応する英語キーワードに置換する
- キーワードが完全に欠落している場合、元のキーワードを再付与する

| 日本語 | 復元先 |
|--------|--------|
| `注:` / `注記:` | `NOTE:` |
| `重要:` | `IMPORTANT:` |
| `ヒント:` | `TIP:` |
| `警告:` | `WARNING:` |
| `注意:` | `CAUTION:` |

### 9.18 セクション見出しの用語集

ドキュメント全体で頻出するセクション見出しの翻訳を統一するため、用語集を設ける。見出しテキスト (レベルプレフィックス除去後) が用語集に一致する場合、AI 翻訳を呼び出さず用語集の訳語を使用する。

| 英語 | 日本語 |
|------|--------|
| See Also | 参照 |
| Additional Resources | 追加リソース |
| Summary | まとめ |
| Prerequisites | 前提条件 |
| Cleanup | クリーンアップ |
| Overview | 概要 |
| Troubleshooting | トラブルシューティング |
| Next Steps | 次のステップ |
| Best Practices | ベストプラクティス |

`[[anchor_id]]` 付きの見出し (例: `== See Also [[see_also]]`) も対応する。見出しテキスト部分のみを用語集で照合し、アンカーはそのまま保持する。

用語集に一致しない見出しは従来通り AI 翻訳する。用語集の拡張は `sync-translate.py` の `_HEADING_GLOSSARY` 辞書に追加する。

### 9.19 コードブロックフェンスの保護

`code_block` ブロックのコメント翻訳時 (`_has_comments` が True の場合)、AI にコードブロック全体を渡すと `----` フェンスマーカーが除去されるケースがある。特に、マークダウン形式の出力 (`##` 見出し、`|` テーブル) を含むコードブロックで発生しやすい。

#### 対策: フェンス分離方式

`admonition_inline` のキーワード保護、`list_item` のプレフィックス保護と同じパターンで、フェンスマーカーを分離・復元する:

1. **翻訳前**: コードブロックの先頭行 (`----` / `....`) と末尾行 (`----` / `....`) を分離し、内部コンテンツのみを AI に送信
2. **翻訳後**: 元のフェンスマーカーを先頭・末尾に再付与

#### 実装

```python
def _strip_code_fences(lines: list[str]) -> tuple[str, str, list[str]]:
    """コードブロックのフェンスマーカーを分離する。"""
    if len(lines) < 2:
        return "", "", lines
    first = lines[0].strip()
    last = lines[-1].strip()
    if (first.startswith("----") or first.startswith("....")) and \
       (last.startswith("----") or last.startswith("......")):
        return lines[0], lines[-1], lines[1:-1]
    return "", "", lines

def _restore_code_fences(
    opening: str, closing: str, lines: list[str]
) -> list[str]:
    """翻訳後のコンテンツにフェンスマーカーを再付与する。"""
    if not opening:
        return lines
    return [opening] + lines + [closing]
```

#### 適用箇所

- `_translate_new_file` 内のコードブロックコメント翻訳パス
- `_translate_file` 内のコードブロックコメント翻訳パス

フェンス分離は `_has_comments` の判定にも影響する。`_has_comments` にはフェンス分離後の内部コンテンツを渡すことで、フェンスマーカー自体をコメント判定の対象外とする。

### 9.20 既存翻訳の事後補正 (アドモニション・見出し用語集)

9.17 (アドモニションキーワード保護) と 9.18 (見出し用語集) は翻訳実行時に適用されるが、upstream で変更がなく再翻訳されなかったブロックには効かない。これらの保護機能が追加される前に翻訳されたブロックに残る不整合を修正するため、翻訳実行時に全ファイルを対象とした事後補正パスを実行する。

#### 処理内容

`sync-translate.py` の処理フローに **ステップ 10a** (ステップ 10 の書式整形後、ステップ 11 の sync-mark 前) を追加する:

1. `modules/*/pages/*.adoc` の全ファイルを走査する
2. 各ファイルをブロックパーサで解析する
3. 各ブロックに対して以下を適用する:
   - `admonition_inline` ブロック: `_ensure_admonition_prefix` を適用し、日本語に翻訳されたアドモニションキーワードを英語に復元する
   - `prose` ブロック: 先頭行が日本語アドモニションキーワード (`重要:`, `ヒント:`, `注:`, `警告:`, `注意:`, `注記:`) で始まる場合、対応する英語キーワードに復元し、ブロック種別を `admonition_inline` に変更する。ブロックパーサは日本語キーワードを認識しないため `prose` として解析されるが、本来は `admonition_inline` であるべきブロック
   - `section_header` ブロック: まず現在の JA 見出しテキストを用語集で照合する。一致しない場合、対応する `originals/` のスナップショットから英語見出しを取得し、英語テキストが用語集に一致すればその訳語を適用する
4. いずれかのブロックが変更された場合のみファイルを書き出す

#### 冪等性

この処理は冪等である。既に正しいキーワード/見出しを持つブロックには変更を加えない。

#### 出力

修正が行われた場合:

```
POSTFIX     modules/networking/pages/static-ip-configuration.adoc (2 blocks corrected)
```

修正なしの場合は出力しない。

### 9.21 Gemini API 呼び出しの効率化

#### 背景

現行の実装は 1 ブロック = 1 API コールの逐次処理であり、大量のブロックを翻訳する場合にレート制限 (RPM: Requests Per Minute、TPM: Tokens Per Minute) がボトルネックとなる。本セクションでは、API 呼び出しを効率化するための変更を規定する。

#### 9.21.1 REST API 直接呼び出しへの移行

`google-genai` SDK を廃止し、標準ライブラリ (`urllib.request`) のみで Gemini REST API を直接呼び出す。これにより外部パッケージ依存が完全になくなり、`.venv` や `pip install` なしで実行可能となる。

##### エンドポイント

```
POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}
```

##### リクエストボディ

```json
{
  "contents": [
    {
      "parts": [
        {"text": "{prompt}"}
      ]
    }
  ]
}
```

##### レスポンス

```json
{
  "candidates": [
    {
      "content": {
        "parts": [
          {"text": "{translated_text}"}
        ]
      }
    }
  ]
}
```

翻訳テキストは `response["candidates"][0]["content"]["parts"][0]["text"]` から取得する。

##### レート制限ヘッダー

レスポンスの HTTP ヘッダーから以下の情報を取得する:

| ヘッダー | 型 | 内容 |
|---|---|---|
| `x-ratelimit-limit-requests` | int | RPM 上限 |
| `x-ratelimit-limit-tokens` | int | TPM 上限 |
| `x-ratelimit-remaining-requests` | int | 残り RPM |
| `x-ratelimit-remaining-tokens` | int | 残り TPM |
| `x-ratelimit-reset-requests` | duration | RPM リセットまでの時間 |
| `x-ratelimit-reset-tokens` | duration | TPM リセットまでの時間 |

初回リクエストのレスポンスで RPM/TPM 上限を取得し、以降のバッチサイズと並列度の計算に使用する。

##### エラー処理

| HTTP ステータス | 対処 |
|---|---|
| 200 | 正常。レスポンスボディからテキストを取得 |
| 429 | レート制限超過。`Retry-After` ヘッダーまたは指数バックオフで待機してリトライ |
| 500, 503 | サーバーエラー。指数バックオフでリトライ (最大 3 回) |
| その他 4xx | 致命的エラー。該当ブロックをスキップ |

#### 9.21.2 ブロックバッチ化

複数の翻訳対象ブロックを 1 回の API コールにまとめることで、RPM 消費を削減する。

##### バッチサイズの決定

初回 API レスポンスから取得した RPM/TPM 上限に基づき、バッチサイズを自動計算する:

```python
batch_size = max(1, min(
    rpm_limit // 2,       # RPM の半分をバッチ削減の余裕に
    tpm_limit // 4000,    # 1ブロック平均 ~4000 トークンと仮定
    15,                   # レスポンスパース信頼性の上限
))
```

RPM/TPM が取得できない場合 (ヘッダーが存在しない場合) は、デフォルト `batch_size = 5` を使用する。

##### バッチプロンプト形式

```
あなたは技術文書の翻訳者です。OpenShift Virtualization に関する英語ドキュメントを日本語に翻訳してください。

## ルール
- (既存のルールと同一)
- 各ブロックは ===BLOCK_N=== で区切られています
- 翻訳結果も同じ ===BLOCK_N=== 区切りで出力してください
- ブロック数を変えないでください

===BLOCK_1===
{english_block_1}

===BLOCK_2===
{english_block_2}

===BLOCK_3===
{english_block_3}
```

##### レスポンスのパース

1. `===BLOCK_N===` デリミタで分割する
2. 入力ブロック数と出力ブロック数が一致することを検証する
3. 不一致の場合は **フォールバック**: バッチ内の各ブロックを個別に再翻訳する (1 ブロック 1 コール)
4. 空のブロックが返された場合も個別再翻訳にフォールバックする

##### バッチ化の適用範囲

| 翻訳パス | バッチ化 | 理由 |
|---|---|---|
| 新規ファイル翻訳 (`_translate_new_file`) | 適用 | 同一ファイル内の連続ブロックをバッチ化 |
| outdated/new ブロック翻訳 (`_translate_file`) | 適用 | 同一ファイル内の翻訳対象ブロックをバッチ化 |
| コードブロックコメント翻訳 | 適用しない | フェンス strip/restore と組み合わせが複雑 |
| nav.adoc の xref 翻訳 | 適用しない | 通常は少数 |
| 見出し翻訳 (用語集不一致時) | バッチに含める | prose 等と同じバッチに混在可 |

##### バッチ化と既存保護機能の統合

- `admonition_inline`: バッチ化前にキーワードを strip し、レスポンスパース後に restore する (ブロック単位で strip/restore を適用)
- `list_item`: バッチ化前にプレフィックスを strip し、レスポンスパース後に restore する
- `section_header`: 用語集に一致するものはバッチに含めない (AI 呼び出し不要)
- `code_block`: バッチに含めない (個別処理を維持)

#### 9.21.3 適応的レート制御

レスポンスヘッダーの残りクォータに基づき、リクエスト間隔を動的に制御する。

##### アルゴリズム

```python
def _adaptive_wait(remaining_rpm: int, remaining_tpm: int, rpm_limit: int):
    """残りクォータに基づいて待機時間を計算する。"""
    if remaining_rpm <= 0 or remaining_tpm <= 0:
        return 60.0  # リセットまで最大 60 秒待機
    if remaining_rpm < rpm_limit * 0.2:
        # 残り 20% 以下: リクエスト間隔を長くする
        return 60.0 / remaining_rpm
    return 0.0  # クォータに余裕がある場合は待機しない
```

##### 429 エラー時の処理

1. `Retry-After` ヘッダーがあればその秒数だけ待機する
2. ヘッダーがなければ指数バックオフ: 4 秒、8 秒、16 秒
3. 3 回連続で 429 の場合、**バッチサイズを半減** して再試行する (TPM 超過の可能性)

##### 進捗表示

```
RATE LIMIT  RPM: 45/1000, TPM: 850000/4000000 (batch_size=10)
```

レート制限情報を 10 リクエストごと (またはバッチごと) に表示する。

#### 9.21.4 並列ファイル処理 (将来拡張)

本バージョンではファイル単位の並列化は実装しない。ブロックバッチ化とレート制御の効果を確認した後、必要に応じて `concurrent.futures.ThreadPoolExecutor` による並列化を検討する。並列化する場合、レート制御はスレッド間で共有する必要がある (`threading.Lock` で保護)。

#### 9.21.5 前提条件の変更

REST API 直接呼び出しへの移行に伴い、以下が変更される:

| 項目 | 変更前 | 変更後 |
|---|---|---|
| 外部パッケージ | `google-genai` 必須 | 不要 (標準ライブラリのみ) |
| セットアップ | `pip install google-genai` | 不要 |
| 仮想環境 | `.venv` 推奨 | 不要 |
| 環境変数 | `GEMINI_API_KEY` | `GEMINI_API_KEY` (変更なし) |
| Python バージョン | 3.9+ | 3.9+ (変更なし) |

### 9.22 既存ファイル更新時のセクション重複防止

#### 背景

`_process_file` は upstream のブロック順にイテレートし、各ブロックについて:
- 旧スナップショットに存在する (synced/outdated) → 旧 JA の対応ブロックを使用
- 旧スナップショットに存在しない (new) → AI で翻訳

しかし upstream でセクションの追加・削除・並べ替えが発生した場合、以下の問題が発生する:

1. **セクション重複**: `match_blocks` (旧スナップショット ↔ JA) のギャップ内位置照合が不正確になり、synced ブロックの JA 取得先がずれる。結果として同一見出しのセクションがファイル内に複数回出現する
2. **見出しレベル不一致**: 新規ブロックの翻訳時に見出しレベル (`===` vs `==`) が保持されない場合がある
3. **見出し内容ズレ**: synced の section_header で旧 JA の別セクション見出しが使用される

#### 対策 1: 見出しレベルの強制保持

`section_header` ブロックの翻訳結果に対して、元の EN 見出しのレベルプレフィックス (`==`, `===` 等) を強制的に復元する:

```python
def _ensure_heading_level(en_lines: list[str], ja_lines: list[str]) -> list[str]:
    """翻訳結果の見出しレベルを EN と一致させる。"""
    if not en_lines or not ja_lines:
        return ja_lines
    en_line = en_lines[0]
    ja_line = ja_lines[0]
    en_prefix = re.match(r'^(={1,5})\s', en_line)
    ja_prefix = re.match(r'^(={1,5})\s', ja_line)
    if en_prefix and ja_prefix and en_prefix.group(1) != ja_prefix.group(1):
        ja_line = en_prefix.group(1) + ja_line[len(ja_prefix.group(1)):]
        return [ja_line] + ja_lines[1:]
    return ja_lines
```

適用箇所: `_process_file` 内の全 section_header 翻訳パス (new, outdated, structure-fix)。用語集適用時にも EN 見出しレベルを使用する。

#### 対策 2: 再構築結果のセクション重複検出・除去

`_process_file` の `new_contents` 構築後、ファイル書き出し前に重複セクションを検出・除去する:

1. `new_contents` から `section_header` のリスト (見出しテキスト、位置) を抽出する
2. 同一見出しテキスト (レベルプレフィックス除去後) が複数回出現する場合:
   a. 対応する EN upstream の見出しテキストを位置から逆引きし、正しい見出しを特定する
   b. 2 回目以降の出現は重複と判定し、その section_header と配下ブロック (次の同レベル以上 section_header まで) を `new_contents` から除去する
3. 重複除去が行われた場合、ログに `DEDUP` を出力する

#### 対策 3: 事後補正パスでの重複除去

事後補正パス (9.20) にもセクション重複検出を追加する。これにより、今回のバグで作成された既存翻訳ファイルの重複も修正できる:

1. 各 `.adoc` ファイルを読み込み、`== ` で始まる行を抽出する
2. 同一テキストの重複を検出する
3. 重複セクション (2 回目以降) とその配下ブロックを除去する
4. `originals/` の対応する EN ファイルのセクション数と一致することを検証する

### 9.23 アドモニション形式・ケースの保持

#### 背景

AI 翻訳時に以下の意図しない変換が発生する:

1. **インライン → ブロック変換**: `NOTE: text` が `[NOTE]\n====\n...\n====` に変換される
2. **ケース昇格**: EN の `Note:` (通常テキスト、非 AsciiDoc アドモニション) が JA で `NOTE:` (AsciiDoc アドモニション) に変換される

#### 対策 1: プロンプト強化 (9.17 の補強)

既存の翻訳プロンプトに以下のルールを追加する:

- 「`Note:` (小文字 n) は通常テキストであり、AsciiDoc のアドモニションキーワード `NOTE:` (大文字) に変換しないこと」
- 「同様に `Tip:`, `Important:`, `Warning:`, `Caution:` も小文字始まりの場合は大文字に変換しないこと」

#### 対策 2: ケース保持のポストプロセス

翻訳結果の先頭が `NOTE: ` / `TIP: ` 等で始まり、EN 原文の先頭が `Note: ` / `Tip: ` 等 (小文字) で始まる場合、翻訳結果を元のケースに復元する。

適用箇所: `_process_file` と `_translate_new_file` の翻訳結果処理。

### 9.24 document_header の属性保持

#### 背景

`document_header` ブロック (ファイル先頭の `= Title` と `:attr:` 行) の翻訳時に、`:navtitle:` 等の属性行が欠落するケースがある。

#### 対策

`document_header` の翻訳結果を処理する際、EN 原文の `:attr:` 行で翻訳結果に存在しないものを復元する。`:navtitle:` の値が英語のまま残っている場合は翻訳結果の値を使用する。

### 9.25 synced パスのセクションヘッダー検証と見出し構造強制

#### 背景

`_process_file` の「synced」パス (EN 未変更 → 旧 JA をそのまま保持) に以下の欠陥がある:

1. **見出しレベル未強制**: `_ensure_heading_level` が適用されないため、旧 JA の見出しレベルが EN と異なっていてもそのまま出力される
2. **誤訳内容の保持**: 旧 JA の section_header に誤った翻訳 (例: `== Cloning vs Golden Images` → `== 前提条件`) が含まれていても、EN 側が変更されていなければ「synced」として保持される
3. **連鎖的レベル不一致**: 誤訳保持により `_dedup_sections` が正しい方の重複を除去し、後続の見出しレベルが全てカスケード的にズレる

実測では 10 ファイル / 73 箇所の見出しレベル不一致と 9 ファイルのセクション欠落・重複が発生した。

#### 対策 1: synced パスの section_header 検証

`_process_file` の synced パスで section_header ブロックを保持する際、以下の検証を行う:

1. `_ensure_heading_level` を適用し、JA 見出しレベルを EN と一致させる
2. JA 見出しテキスト (レベルプレフィックス除去後) が用語集 `_HEADING_GLOSSARY` の値 (他の EN 見出しの翻訳) と一致し、かつ対応する EN 見出しテキストがその用語集エントリのキーと異なる場合、**誤訳として再翻訳する**

```python
# synced パスの section_header 処理
kept_lines = list(ja_blocks[ja_idx].lines)
kept_lines = _ensure_heading_level(list(up_block.lines), kept_lines)

# 誤訳検出: JA テキストが別の EN 見出しの用語集翻訳と重複
ja_text = _heading_text(kept_lines)
en_text = _heading_text(list(up_block.lines))
expected_ja = _HEADING_GLOSSARY.get(en_text)
if expected_ja is None and ja_text in _HEADING_GLOSSARY.values():
    # 用語集にない EN 見出しなのに JA が用語集翻訳と一致 → 誤訳
    # → AI で再翻訳
```

#### 対策 2: ポスト再構築の見出し構造検証

`_process_file` で `new_contents` を構築した後、`_dedup_sections` の後に見出し構造を EN と比較検証する:

1. EN と JA の見出しレベルシーケンスを位置順に取得
2. 見出し数が一致し、レベルシーケンスも一致すれば何もしない (高速パス)
3. 見出し数は一致するがレベルが異なる場合: 位置ベースで EN のレベルを強制適用

```python
def _enforce_heading_structure(new_contents, up_blocks):
    en_headers = [(i, b) for i, b in enumerate(up_blocks)
                  if b.block_type == "section_header"]
    ja_headers = [(i, lines) for i, (bt, lines) in enumerate(new_contents)
                  if bt == "section_header"]
    if len(en_headers) != len(ja_headers):
        return new_contents  # 数不一致は _dedup_sections で対応
    for (_, en_b), (ja_i, ja_lines) in zip(en_headers, ja_headers):
        en_level = _heading_level(list(en_b.lines))
        ja_level = _heading_level(ja_lines)
        if en_level != ja_level:
            ja_lines[0] = re.sub(r'^=+', '=' * en_level, ja_lines[0])
            new_contents[ja_i] = ("section_header", ja_lines)
    return new_contents
```

`_translate_new_file` でも同様に構築後に呼び出す。

#### 適用順序

`_process_file` 内:
1. `new_contents` 構築 (全ブロックイテレーション)
2. `_dedup_sections(new_contents, up_blocks)` — テキスト重複除去
3. `_enforce_heading_structure(new_contents, up_blocks)` — レベル強制 (**新規**)
4. `_reconstruct_file(new_contents)` — ファイル書き出し

---

## 10. 既存翻訳の初期化手順

本リポジトリには、ツール導入前に翻訳済みのファイルが 53 件 (他に JA 独自ファイル 5 件) 存在する。これらを正しく管理対象に登録するには、**翻訳元となった時点の upstream コンテンツ** を基準にスナップショットを取る必要がある。

### 10.1 背景

本リポジトリは upstream コミット `70218468` (2026-06-09) 時点の英語コンテンツを元に、2026-06-12 に AI (RCB/Gemini) で一括翻訳された。`sync-init.py` のデフォルト (`--upstream-ref upstream/main`) で初期化すると最新の upstream を基準にしてしまい、fork 以降に upstream で追加・変更されたコンテンツも `synced` 扱いになる。

### 10.2 初期化手順

```bash
# 1. upstream remote を追加・フェッチ
git remote add upstream https://github.com/RedHatQuickCourses/ocp-virt-cookbook.git
git fetch upstream

# 2. fork 時点の upstream コミットを基準にスナップショットを取得
python3 tools/translation/sync-init.py --upstream-ref 70218468

# 3. (参考) 構造一致バリデーションで AI 翻訳時の欠損・余剰を確認
python3 tools/translation/validate-structure.py

# 4. AI で差分を一括翻訳し、日本語ファイルに適用 + sync-mark.py 自動実行
#    構造不一致 (欠損セクション・余剰ブロック) も自動修正される
python3 tools/translation/sync-translate.py
```

### 10.3 各ステップの説明

**ステップ 2** により、fork 時点の英語原文がスナップショットとして保存され、全ブロックが `synced` として登録される。この時点でのマニフェストは「翻訳時点では全て対応済み」という状態を表す。

**ステップ 3 (参考)** により、AI 翻訳時に欠落したセクション (例: `vm-lifecycle-states.adoc` の `== See Also`) や余剰ブロック (例: `vm-templates.adoc` の余分な NOTE ブロック)、コードブロック内コメントの誤翻訳を事前に確認できる。ステップ 4 で全て自動修正されるため、手動対応は不要。

**ステップ 4** (`sync-translate.py`) により、以下が自動的に処理される:
- **構造不一致の自動修正** (処理フロー 7d2): 欠損ブロック (スナップショットに存在するが JA にない) は翻訳して挿入、余剰ブロック (JA にあるがスナップショットにない) は削除。これにより 1:1 のブロック対応が確立される
- **upstream 差分の翻訳**: fork 以降の `outdated` / `new` ブロックが AI で翻訳される
- **コードブロック修正**: コメントの誤翻訳が英語原文に自動復元される
- **attachments/images の同期**: 変更された YAML マニフェストや画像が upstream で上書きされる
- 書式整形ツールの自動実行と `sync-mark.py` によるマニフェスト更新も行われる

### 10.4 初期化後の状態

```
=== 初期化直後の想定状態 ===

sync-init.py 実行後:
  管理対象ファイル:      53 / 77 upstream page files
  全同期済み (fork時点):  53  ← sync-init.py で登録済み (JA独自5件はスキップ)
  構造不一致:             数件  ← AI 翻訳時の欠損・余剰 (sync-translate.py で自動修正)

sync-translate.py 実行後:
  構造不一致:             0件  ← 欠損ブロック挿入、余剰ブロック削除済み
  outdated/new ブロック:  0件  ← fork 以降の変更を全て翻訳済み
  新規ファイル:           24件翻訳済み  ← fork 以降に upstream で追加されたページファイル
  新規 attachments:       55件コピー  ← 新規ページに対応する YAML/スクリプト等
  新規 images:            24件コピー  ← 新規ページおよび既存ページ向けの画像
  attachments 更新:       17件  ← fork 以降に upstream で変更された既存ファイル
```

### 10.5 注意事項

- `--upstream-ref 70218468` は **初回の初期化時のみ** 使用する。以降の `sync-check.py` / `sync-mark.py` は `upstream/main` (デフォルト) を使用する。
- fork 後にこのリポジトリ側でレビュー・修正を加えたファイル (例: `first_review` ブランチでの変更) は、日本語テキストが更新されているが、スナップショットの基準は fork 時点の英語のままとなる。これは正しい動作であり、upstream 側の変更のみを追跡する設計に合致する。
- AI 翻訳時の構造不一致 (欠損セクション、余剰ブロック、コードブロック内コメント翻訳) は全て `sync-translate.py` が自動修正する。手動対応は不要。
