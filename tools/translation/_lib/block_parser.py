#!/usr/bin/env python3
"""AsciiDoc ブロック解析エンジン。

翻訳同期ツールの中核モジュール。AsciiDoc ファイルをブロック単位に分解し、
各ブロックに一意の ID とコンテンツハッシュを付与する。

SYNC-SPEC.md セクション 2 (ブロック解析アルゴリズム)、セクション 3 (ブロック ID 体系)、
セクション 4 (ハッシュ計算) の実装。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """AsciiDoc ファイル内の意味的に独立した最小単位。

    Attributes:
        block_type: ブロック種別 (document_header, section_header, prose 等)。
        lines: ブロックの生テキスト行のリスト。
        start_line: ファイル内の開始行番号 (1-indexed)。
        end_line: ファイル内の終了行番号 (1-indexed)。
        block_id: 生成されたブロック ID (section_path/block_type/ordinal)。
        section_path: このブロックが属するセクションパス。
        attrs: グルーピングされた block_attribute 行のリスト。
        title: グルーピングされた block_title 行 (存在しない場合は None)。
    """

    block_type: str
    lines: list[str]
    start_line: int
    end_line: int
    block_id: str = ""
    section_path: str = ""
    attrs: list[str] = field(default_factory=list)
    title: str | None = None


class _State(Enum):
    """解析ステートマシンの状態。"""

    NORMAL = auto()
    IN_CODE_BLOCK = auto()
    IN_LITERAL_BLOCK = auto()
    IN_EXAMPLE_BLOCK = auto()
    IN_TABLE = auto()
    EXPECT_DELIMITED = auto()
    EXPECT_TITLED_BLOCK = auto()


# ---------------------------------------------------------------------------
# 正規表現パターン
# ---------------------------------------------------------------------------

_RE_CODE_DELIM = re.compile(r"^----\s*$")
_RE_LITERAL_DELIM = re.compile(r"^\.\.\.\.\s*$")
_RE_EXAMPLE_DELIM = re.compile(r"^====\s*$")
_RE_TABLE_DELIM = re.compile(r"^\|===\s*$")

_RE_SECTION_HEADER = re.compile(r"^(={2,5})\s")
_RE_DOCUMENT_HEADER = re.compile(r"^= \S")
_RE_ATTRIBUTE_ENTRY = re.compile(r"^:[a-zA-Z].*:")
_RE_BLOCK_ATTRIBUTE = re.compile(r"^\[.*\]$")
_RE_BLOCK_TITLE = re.compile(r"^\.[A-Za-z　-鿿]")
_RE_ADMONITION_INLINE = re.compile(
    r"^(NOTE|WARNING|IMPORTANT|CAUTION|TIP): "
)
_RE_LIST_ITEM = re.compile(r"^(\*+|\.+)\s|^.+::")
_RE_LIST_CONTINUATION = re.compile(r"^\+\s*$")

# block_attribute の特殊パターン (EXPECT_DELIMITED への遷移判定用)
_RE_SOURCE_ATTR = re.compile(r"^\[source[,\]]")
_RE_COLS_ATTR = re.compile(r"^\[cols[=\]]")
_RE_ADMONITION_ATTR = re.compile(
    r"^\[(NOTE|WARNING|IMPORTANT|CAUTION|TIP)\]$"
)

# アンカー抽出用
_RE_ANCHOR_BRACKET = re.compile(r"^\[\[([^\]]+)\]\]$")
_RE_ANCHOR_SHORTHAND = re.compile(r"^\[#([^\]]+)\]$")


# ---------------------------------------------------------------------------
# ブロック解析
# ---------------------------------------------------------------------------

def parse_blocks(content: str) -> list[Block]:
    """AsciiDoc コンテンツをブロックのリストに解析する。

    SYNC-SPEC.md セクション 2.4 のステートマシンに基づく実装。

    Args:
        content: AsciiDoc ファイルの全テキスト。

    Returns:
        解析されたブロックのリスト。
    """
    if not content:
        return []

    raw_lines = content.split("\n")
    # 末尾の空文字列を除去 (ファイル末尾の改行による)
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    blocks: list[Block] = []
    state = _State.NORMAL

    # 現在蓄積中のブロック
    current_lines: list[str] = []
    current_type: str = ""
    current_start: int = 0

    # グルーピング用の蓄積バッファ
    pending_attrs: list[str] = []
    pending_attrs_lines: list[str] = []  # 属性行の生テキスト
    pending_attrs_start: int = 0
    pending_title: str | None = None
    pending_title_line: str | None = None
    pending_title_start: int = 0

    # リスト継続状態
    in_list_continuation = False
    list_continuation_code_delim: str | None = None  # +後のコードブロック追跡

    def _finalize_block(
        btype: str,
        lines: list[str],
        start: int,
        end: int,
        attrs: list[str] | None = None,
        attr_lines: list[str] | None = None,
        title: str | None = None,
        title_line: str | None = None,
        title_start: int = 0,
        attr_start: int = 0,
    ) -> None:
        """ブロックを確定してリストに追加する。"""
        if not lines and not attr_lines and title_line is None:
            return

        all_lines: list[str] = []
        actual_start = start

        # attr_lines がある場合、ブロックの先頭に含める
        if attr_lines:
            all_lines.extend(attr_lines)
            if attr_start > 0:
                actual_start = min(actual_start, attr_start) if start > 0 else attr_start

        # title_line がある場合
        if title_line is not None:
            all_lines.append(title_line)
            if title_start > 0:
                actual_start = min(actual_start, title_start) if actual_start > 0 else title_start

        all_lines.extend(lines)

        if actual_start == 0 and all_lines:
            actual_start = start

        block = Block(
            block_type=btype,
            lines=all_lines,
            start_line=actual_start,
            end_line=end,
            attrs=attrs if attrs else [],
            title=title,
        )
        blocks.append(block)

    def _flush_current() -> None:
        """現在蓄積中のブロックを確定する。"""
        nonlocal current_lines, current_type, current_start, in_list_continuation
        nonlocal list_continuation_code_delim
        if current_lines and current_type:
            _finalize_block(
                current_type,
                current_lines,
                current_start,
                current_start + len(current_lines) - 1,
            )
        current_lines = []
        current_type = ""
        current_start = 0
        in_list_continuation = False
        list_continuation_code_delim = None

    def _flush_pending_attrs_as_blocks() -> None:
        """蓄積中の block_attribute / block_title を単独ブロックとして確定する。"""
        nonlocal pending_attrs, pending_attrs_lines, pending_attrs_start
        nonlocal pending_title, pending_title_line, pending_title_start
        line_idx = pending_attrs_start
        for i, attr_line in enumerate(pending_attrs_lines):
            _finalize_block(
                "block_attribute",
                [attr_line],
                line_idx + i,
                line_idx + i,
            )
        if pending_title_line is not None:
            _finalize_block(
                "block_title",
                [pending_title_line],
                pending_title_start,
                pending_title_start,
            )
        _clear_pending()

    def _clear_pending() -> None:
        """蓄積バッファをクリアする。"""
        nonlocal pending_attrs, pending_attrs_lines, pending_attrs_start
        nonlocal pending_title, pending_title_line, pending_title_start
        pending_attrs = []
        pending_attrs_lines = []
        pending_attrs_start = 0
        pending_title = None
        pending_title_line = None
        pending_title_start = 0

    def _has_pending() -> bool:
        return bool(pending_attrs_lines) or pending_title_line is not None

    def _collect_pending() -> tuple[list[str], list[str], str | None, str | None, int, int]:
        """蓄積中の属性/タイトル情報を返してクリアする。"""
        a = pending_attrs[:]
        al = pending_attrs_lines[:]
        t = pending_title
        tl = pending_title_line
        ts = pending_title_start
        a_start = pending_attrs_start
        _clear_pending()
        return a, al, t, tl, ts, a_start

    # 行番号は 1-indexed
    line_num = 0
    reprocess_line: tuple[int, str] | None = None

    while True:
        # 再処理対象の行があればそれを使う
        if reprocess_line is not None:
            line_num, line = reprocess_line
            reprocess_line = None
        else:
            line_num += 1
            if line_num > len(raw_lines):
                break
            line = raw_lines[line_num - 1]

        # ----- リスト継続中のデリミタブロック処理 -----
        if list_continuation_code_delim is not None:
            current_lines.append(line)
            current_end = line_num
            is_closing = (
                (list_continuation_code_delim == "----" and _RE_CODE_DELIM.match(line))
                or (list_continuation_code_delim == "...." and _RE_LITERAL_DELIM.match(line))
                or (list_continuation_code_delim == "|===" and _RE_TABLE_DELIM.match(line))
            )
            if is_closing:
                list_continuation_code_delim = None
            continue

        # ----- ステートマシン -----

        if state == _State.IN_CODE_BLOCK:
            current_lines.append(line)
            if _RE_CODE_DELIM.match(line):
                a, al, t, tl, ts, a_s = _collect_pending()
                _finalize_block(
                    "code_block", current_lines, current_start, line_num,
                    attrs=a, attr_lines=al, title=t, title_line=tl,
                    title_start=ts, attr_start=a_s,
                )
                current_lines = []
                current_type = ""
                current_start = 0
                state = _State.NORMAL
            continue

        if state == _State.IN_LITERAL_BLOCK:
            current_lines.append(line)
            if _RE_LITERAL_DELIM.match(line):
                a, al, t, tl, ts, a_s = _collect_pending()
                _finalize_block(
                    "literal_block", current_lines, current_start, line_num,
                    attrs=a, attr_lines=al, title=t, title_line=tl,
                    title_start=ts, attr_start=a_s,
                )
                current_lines = []
                current_type = ""
                current_start = 0
                state = _State.NORMAL
            continue

        if state == _State.IN_EXAMPLE_BLOCK:
            current_lines.append(line)
            if _RE_EXAMPLE_DELIM.match(line):
                a, al, t, tl, ts, a_s = _collect_pending()
                _finalize_block(
                    "example_block", current_lines, current_start, line_num,
                    attrs=a, attr_lines=al, title=t, title_line=tl,
                    title_start=ts, attr_start=a_s,
                )
                current_lines = []
                current_type = ""
                current_start = 0
                state = _State.NORMAL
            continue

        if state == _State.IN_TABLE:
            current_lines.append(line)
            if _RE_TABLE_DELIM.match(line):
                a, al, t, tl, ts, a_s = _collect_pending()
                _finalize_block(
                    "table", current_lines, current_start, line_num,
                    attrs=a, attr_lines=al, title=t, title_line=tl,
                    title_start=ts, attr_start=a_s,
                )
                current_lines = []
                current_type = ""
                current_start = 0
                state = _State.NORMAL
            continue

        if state == _State.EXPECT_DELIMITED:
            if _RE_CODE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_CODE_BLOCK
                continue
            if _RE_EXAMPLE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_EXAMPLE_BLOCK
                continue
            if _RE_TABLE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_TABLE
                continue
            if _RE_LITERAL_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_LITERAL_BLOCK
                continue
            # block_title も蓄積可能 (spec: EXPECT_DELIMITED で .テキスト)
            if _RE_BLOCK_TITLE.match(line) and not _RE_LITERAL_DELIM.match(line):
                pending_title = line
                pending_title_line = line
                pending_title_start = line_num
                # EXPECT_DELIMITED のまま
                continue
            # それ以外 -> block_attribute を単独ブロックとして確定し、行を再処理
            _flush_pending_attrs_as_blocks()
            state = _State.NORMAL
            reprocess_line = (line_num, line)
            continue

        if state == _State.EXPECT_TITLED_BLOCK:
            if _RE_CODE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_CODE_BLOCK
                continue
            if _RE_EXAMPLE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_EXAMPLE_BLOCK
                continue
            if _RE_LITERAL_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_LITERAL_BLOCK
                continue
            if _RE_TABLE_DELIM.match(line):
                _flush_current()
                current_lines = [line]
                current_start = line_num
                state = _State.IN_TABLE
                continue
            # block_attribute を蓄積して EXPECT_DELIMITED へ
            if _RE_BLOCK_ATTRIBUTE.match(line):
                if not pending_attrs_lines:
                    pending_attrs_start = line_num
                pending_attrs.append(line)
                pending_attrs_lines.append(line)
                state = _State.EXPECT_DELIMITED
                continue
            # それ以外 -> block_title を単独ブロックとして確定し、行を再処理
            _flush_pending_attrs_as_blocks()
            state = _State.NORMAL
            reprocess_line = (line_num, line)
            continue

        # ----- state == NORMAL -----

        # 空行: ブロック区切り
        if line.strip() == "":
            _flush_current()
            continue

        # リスト継続 (+)
        if _RE_LIST_CONTINUATION.match(line) and current_type == "list_item":
            current_lines.append(line)
            in_list_continuation = True
            continue

        # リスト継続中の蓄積
        if in_list_continuation and current_type == "list_item":
            # コードブロック開始
            if _RE_CODE_DELIM.match(line):
                current_lines.append(line)
                list_continuation_code_delim = "----"
                continue
            if _RE_LITERAL_DELIM.match(line):
                current_lines.append(line)
                list_continuation_code_delim = "...."
                continue
            # テーブル開始
            if _RE_TABLE_DELIM.match(line):
                current_lines.append(line)
                list_continuation_code_delim = "|==="
                continue
            # 新しいリスト項目が始まったら、現在のブロックを確定して再処理
            if _RE_LIST_ITEM.match(line):
                _flush_current()
                reprocess_line = (line_num, line)
                continue
            # それ以外はリスト項目に追加
            current_lines.append(line)
            continue

        # ドキュメントヘッダー (1行目が `= ` で始まる場合)
        if line_num == 1 and _RE_DOCUMENT_HEADER.match(line):
            _flush_current()
            current_lines = [line]
            current_type = "document_header"
            current_start = line_num
            # 後続の attribute_entry 行を含める
            while line_num < len(raw_lines):
                next_line = raw_lines[line_num]  # 0-indexed で line_num
                if _RE_ATTRIBUTE_ENTRY.match(next_line):
                    line_num += 1
                    current_lines.append(next_line)
                elif next_line.strip() == "":
                    break
                else:
                    break
            _finalize_block(
                "document_header",
                current_lines,
                current_start,
                current_start + len(current_lines) - 1,
            )
            current_lines = []
            current_type = ""
            current_start = 0
            continue

        # セクションヘッダー
        m_section = _RE_SECTION_HEADER.match(line)
        if m_section:
            _flush_current()
            # pending がある場合は単独ブロックとして確定
            if _has_pending():
                _flush_pending_attrs_as_blocks()
            _finalize_block(
                "section_header",
                [line],
                line_num,
                line_num,
            )
            continue

        # block_attribute (各種パターン)
        if _RE_BLOCK_ATTRIBUTE.match(line):
            _flush_current()
            if not pending_attrs_lines:
                pending_attrs_start = line_num
            pending_attrs.append(line)
            pending_attrs_lines.append(line)
            # 遷移先の判定
            if (_RE_SOURCE_ATTR.match(line)
                    or _RE_ADMONITION_ATTR.match(line)
                    or _RE_COLS_ATTR.match(line)):
                state = _State.EXPECT_DELIMITED
            else:
                state = _State.EXPECT_DELIMITED
            continue

        # デリミタ行 (block_attribute なしで出現)
        if _RE_CODE_DELIM.match(line):
            _flush_current()
            current_lines = [line]
            current_start = line_num
            state = _State.IN_CODE_BLOCK
            continue

        if _RE_LITERAL_DELIM.match(line):
            _flush_current()
            current_lines = [line]
            current_start = line_num
            state = _State.IN_LITERAL_BLOCK
            continue

        if _RE_EXAMPLE_DELIM.match(line):
            _flush_current()
            current_lines = [line]
            current_start = line_num
            state = _State.IN_EXAMPLE_BLOCK
            continue

        if _RE_TABLE_DELIM.match(line):
            _flush_current()
            current_lines = [line]
            current_start = line_num
            state = _State.IN_TABLE
            continue

        # admonition_inline
        if _RE_ADMONITION_INLINE.match(line):
            _flush_current()
            current_lines = [line]
            current_type = "admonition_inline"
            current_start = line_num
            continue

        # block_title (`.` + 非空白文字、`....` ではない)
        if _RE_BLOCK_TITLE.match(line) and not _RE_LITERAL_DELIM.match(line):
            _flush_current()
            pending_title = line
            pending_title_line = line
            pending_title_start = line_num
            state = _State.EXPECT_TITLED_BLOCK
            continue

        # attribute_entry (ドキュメントヘッダー外)
        if _RE_ATTRIBUTE_ENTRY.match(line):
            _flush_current()
            _finalize_block(
                "attribute_entry",
                [line],
                line_num,
                line_num,
            )
            continue

        # list_item
        if _RE_LIST_ITEM.match(line):
            # 同一種別のリスト項目を続けて蓄積する場合は新しいブロック
            if current_type == "list_item":
                _flush_current()
            elif current_type and current_type != "list_item":
                _flush_current()
            current_type = "list_item"
            if not current_lines:
                current_start = line_num
            current_lines.append(line)
            continue

        # prose (上記のいずれにも該当しない非空行)
        if current_type == "prose":
            current_lines.append(line)
        elif current_type == "admonition_inline":
            # admonition_inline は空行まで続く
            current_lines.append(line)
        elif current_type == "list_item":
            # リスト項目の後続行 (定義リストの説明文など)
            # 空行まで同じ list_item ブロックに含める
            current_lines.append(line)
        else:
            _flush_current()
            current_lines = [line]
            current_type = "prose"
            current_start = line_num

    # ファイル末尾: 残りを確定
    # デリミタブロック内で EOF に達した場合、正しい型で確定する
    _STATE_TO_TYPE = {
        _State.IN_CODE_BLOCK: "code_block",
        _State.IN_LITERAL_BLOCK: "literal_block",
        _State.IN_EXAMPLE_BLOCK: "example_block",
        _State.IN_TABLE: "table",
    }
    if state in _STATE_TO_TYPE and current_lines:
        btype = _STATE_TO_TYPE[state]
        a, al, t, tl, ts, a_s = _collect_pending()
        _finalize_block(
            btype, current_lines, current_start,
            current_start + len(current_lines) - 1,
            attrs=a, attr_lines=al, title=t, title_line=tl,
            title_start=ts, attr_start=a_s,
        )
        current_lines = []
        current_type = ""
        state = _State.NORMAL
    _flush_current()
    if _has_pending():
        _flush_pending_attrs_as_blocks()

    return blocks


# ---------------------------------------------------------------------------
# ブロック ID 生成
# ---------------------------------------------------------------------------

def _fullwidth_to_halfwidth(text: str) -> str:
    """全角英数字を半角に変換する。

    Unicode の NFKC 正規化では不十分なケースがあるため、
    全角英数字の範囲を明示的に変換する。
    """
    result = []
    for ch in text:
        cp = ord(ch)
        # 全角数字 U+FF10-U+FF19 -> 0-9
        if 0xFF10 <= cp <= 0xFF19:
            result.append(chr(cp - 0xFF10 + ord("0")))
        # 全角大文字 U+FF21-U+FF3A -> A-Z
        elif 0xFF21 <= cp <= 0xFF3A:
            result.append(chr(cp - 0xFF21 + ord("A")))
        # 全角小文字 U+FF41-U+FF5A -> a-z
        elif 0xFF41 <= cp <= 0xFF5A:
            result.append(chr(cp - 0xFF41 + ord("a")))
        else:
            result.append(ch)
    return "".join(result)


def _slugify(text: str) -> str:
    """見出しテキストをスラグ化する。

    SYNC-SPEC.md セクション 3.4 のルールに従う:
    1. 先頭の ``=`` 記号とスペースを除去
    2. 全角英数字を半角に変換
    3. 小文字化
    4. 空白を ``-`` に置換
    5. ``[a-z0-9\\u3040-\\u9fff-]`` 以外の文字を除去
    6. 50文字で切り詰め
    """
    # 1. 先頭の = とスペースを除去
    s = re.sub(r"^=+\s*", "", text)
    # 2. 全角英数字を半角に変換
    s = _fullwidth_to_halfwidth(s)
    # 3. 小文字化
    s = s.lower()
    # 4. 空白を - に置換
    s = re.sub(r"\s+", "-", s)
    # 5. 許可文字以外を除去 (a-z, 0-9, ひらがな〜CJK統合漢字, ハイフン)
    s = re.sub(r"[^a-z0-9぀-鿿\-]", "", s)
    # 6. 50文字で切り詰め
    s = s[:50]
    return s


def _extract_anchor_from_attrs(attrs: list[str]) -> str | None:
    """block_attribute リストからアンカー ID を抽出する。

    ``[[anchor_id]]`` 形式または ``[#anchor_id]`` 形式のアンカーを検出する。

    Returns:
        アンカー ID 文字列、見つからなければ None。
    """
    for attr in attrs:
        m = _RE_ANCHOR_BRACKET.match(attr)
        if m:
            return m.group(1)
        m = _RE_ANCHOR_SHORTHAND.match(attr)
        if m:
            return m.group(1)
    return None


def generate_block_ids(blocks: list[Block]) -> None:
    """各ブロックにブロック ID とセクションパスを割り当てる。

    SYNC-SPEC.md セクション 3 に基づき、インプレースで ``block_id`` と
    ``section_path`` フィールドを設定する。

    ID 形式: ``<section_path>/<block_type>/<ordinal>``

    セクションパスは見出しレベルに基づく階層構造を持つ。例えば
    ``== Method 1`` の下の ``=== When to Use`` は
    ``method-1/when-to-use`` となり、同名サブセクションの衝突を防ぐ。

    Args:
        blocks: :func:`parse_blocks` が返したブロックのリスト。
    """
    current_section_path = "_root"
    # 各見出しレベル (2-5) のスラグを保持
    level_slugs: dict[int, str] = {}
    # セクション内のブロック種別ごとのカウンター
    ordinal_counters: dict[str, int] = {}

    for i, block in enumerate(blocks):
        if block.block_type == "section_header":
            heading_text = block.lines[0] if block.lines else ""

            # 見出しレベルを検出 (== → 2, === → 3, ...)
            m = _RE_SECTION_HEADER.match(heading_text)
            level = len(m.group(1)) if m else 2

            # アンカーが attrs にある場合 (ヘッダー直前の [[anchor]] 等)
            anchor = _extract_anchor_from_attrs(block.attrs)

            # attrs がない場合、直前の block_attribute ブロックからアンカーを探す
            if not anchor and i > 0:
                prev = blocks[i - 1]
                if prev.block_type == "block_attribute":
                    anchor = _extract_anchor_from_attrs(prev.lines)

            slug = anchor if anchor else _slugify(heading_text)

            # このレベルのスラグを設定し、より深いレベルをクリア
            level_slugs[level] = slug
            for lvl in [k for k in level_slugs if k > level]:
                del level_slugs[lvl]

            # 全アクティブレベルから階層パスを構築
            path_parts = [level_slugs[lvl]
                          for lvl in sorted(level_slugs)]
            current_section_path = "/".join(path_parts)

            # カウンターをリセット
            ordinal_counters = {}

        block.section_path = current_section_path

        # ordinal の計算
        bt = block.block_type
        ordinal = ordinal_counters.get(bt, 0)
        ordinal_counters[bt] = ordinal + 1

        block.block_id = f"{current_section_path}/{bt}/{ordinal}"


# ---------------------------------------------------------------------------
# ハッシュ計算
# ---------------------------------------------------------------------------

def compute_block_hash(lines: list[str]) -> str:
    """ブロックのコンテンツハッシュを計算する。

    SYNC-SPEC.md セクション 4 に基づく正規化とハッシュ計算。

    - 各行の末尾空白を除去
    - 改行 (``\\n``) で結合
    - SHA-256 の先頭 16 文字を返す

    Args:
        lines: ブロックの生テキスト行のリスト (attrs, title 行を含む)。

    Returns:
        SHA-256 ハッシュの先頭 16 文字 (hex)。
    """
    normalized = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
