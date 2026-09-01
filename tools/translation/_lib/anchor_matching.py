#!/usr/bin/env python3
"""不変アンカー照合モジュール。

英語スナップショットと日本語ファイルのブロックを対応づけ、
変更があった英語ブロックに対応する日本語側の行番号を特定する。

SYNC-SPEC.md セクション 5 (不変アンカー照合アルゴリズム) の実装。
"""

from __future__ import annotations

import difflib
import re

from .block_parser import Block, compute_block_hash


# ---------------------------------------------------------------------------
# フィンガープリント抽出用パターン
# ---------------------------------------------------------------------------

_RE_SECTION_LEVEL = re.compile(r"^(=+)\s")
_RE_INLINE_ANCHOR = re.compile(r"\[\[([^\]]+)\]\]")
_RE_BRACKET_ANCHOR = re.compile(r"^\[\[([^\]]+)\]\]$")
_RE_SHORTHAND_ANCHOR = re.compile(r"^\[#([^\]]+)\]$")
_RE_CODE_DELIM = re.compile(r"^----\s*$")
_RE_LITERAL_DELIM = re.compile(r"^\.\.\.\.\s*$")
_RE_ADMONITION_KEYWORD = re.compile(
    r"^(NOTE|WARNING|IMPORTANT|CAUTION|TIP): "
)
_RE_COLS_ATTR = re.compile(r"^\[cols")
_RE_ADMONITION_ATTR = re.compile(
    r"^\[(NOTE|WARNING|IMPORTANT|CAUTION|TIP)\]$"
)


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------

def _extract_anchor_from_attrs(attrs: list[str]) -> str | None:
    """属性リストからアンカー ID を抽出する。

    ``[[anchor_id]]`` 形式または ``[#anchor_id]`` 形式を検出する。

    Returns:
        アンカー ID 文字列、見つからなければ None。
    """
    for attr in attrs:
        m = _RE_BRACKET_ANCHOR.match(attr)
        if m:
            return m.group(1)
        m = _RE_SHORTHAND_ANCHOR.match(attr)
        if m:
            return m.group(1)
    return None


def _extract_delimited_content(
    lines: list[str], delim_re: re.Pattern[str]
) -> list[str]:
    """デリミタ行に囲まれた内容行を抽出する。

    ブロックの lines リストから、最初のデリミタ行と最後のデリミタ行の間の
    行を返す。attrs 行や title 行 (デリミタより前の行) は除外される。
    """
    first = -1
    last = -1
    for i, line in enumerate(lines):
        if delim_re.match(line):
            if first < 0:
                first = i
            last = i
    if first < 0 or last <= first:
        return []
    return lines[first + 1 : last]


# ---------------------------------------------------------------------------
# フィンガープリント抽出
# ---------------------------------------------------------------------------

def extract_fingerprint(block: Block) -> tuple | None:
    """ブロックから不変フィンガープリントを抽出する。

    不変要素 (翻訳しても変わらない構造的特徴) を持つブロックに対して、
    英語・日本語間で一致するフィンガープリントタプルを返す。
    不変要素を持たないブロックの場合は None を返す。

    フィンガープリントの種類:
        - ``("section", level, anchor_id_or_None)`` — セクション見出し
        - ``("code", content_hash)`` — コードブロック / リテラルブロック
        - ``("attr", raw_text)`` — ブロック属性 (単独)
        - ``("admonition", keyword)`` — インラインアドモニション
        - ``("table", cols_attr)`` — テーブル

    Args:
        block: 解析済みブロック。

    Returns:
        フィンガープリントタプル、または None。
    """
    bt = block.block_type

    # --- section_header ---
    if bt == "section_header":
        line = block.lines[0] if block.lines else ""
        m = _RE_SECTION_LEVEL.match(line)
        level = len(m.group(1)) if m else 0

        # attrs から [[anchor]] / [#anchor] を探す
        anchor = _extract_anchor_from_attrs(block.attrs)

        # 見出しテキスト中のインラインアンカー [[...]] を探す
        if anchor is None:
            m_inline = _RE_INLINE_ANCHOR.search(line)
            if m_inline:
                anchor = m_inline.group(1)

        return ("section", level, anchor)

    # --- code_block ---
    if bt == "code_block":
        content = _extract_delimited_content(block.lines, _RE_CODE_DELIM)
        code_only = [l for l in content if not l.lstrip().startswith("#")]
        return ("code", compute_block_hash(code_only))

    # --- literal_block ---
    if bt == "literal_block":
        content = _extract_delimited_content(block.lines, _RE_LITERAL_DELIM)
        return ("code", compute_block_hash(content))

    # --- block_attribute (単独ブロック) ---
    if bt == "block_attribute":
        raw = block.lines[0] if block.lines else ""
        return ("attr", raw)

    # --- admonition_inline ---
    if bt == "admonition_inline":
        line = block.lines[0] if block.lines else ""
        m = _RE_ADMONITION_KEYWORD.match(line)
        if m:
            return ("admonition", m.group(1))
        return None

    # --- table ---
    if bt == "table":
        cols_attr = ""
        for attr in block.attrs:
            if _RE_COLS_ATTR.match(attr):
                cols_attr = attr
                break
        return ("table", cols_attr)

    # --- example_block ---
    if bt == "example_block":
        for attr in block.attrs:
            if _RE_ADMONITION_ATTR.match(attr):
                return ("attr", attr)
        return None

    # その他のブロック種別は不変要素なし
    return None


# ---------------------------------------------------------------------------
# ブロック照合
# ---------------------------------------------------------------------------

def match_blocks(
    en_blocks: list[Block], ja_blocks: list[Block]
) -> list[tuple[int, int]]:
    """EN ブロックと JA ブロックを不変アンカーと位置で照合する。

    **パス 1 (アンカーベース)**: 両ブロック列からフィンガープリントを抽出し、
    ``difflib.SequenceMatcher`` で最長共通部分列 (LCS) を求めて固定点を確立する。

    **パス 2 (ギャップ内位置照合)**: 固定点間のギャップにあるブロックを
    出現順で 1:1 に対応づける。ギャップ内のブロック数が EN と JA で異なる場合は、
    少ない方に合わせて対応づけ、残りは未対応とする。

    Args:
        en_blocks: 英語ブロックのリスト。
        ja_blocks: 日本語ブロックのリスト。

    Returns:
        ``(en_index, ja_index)`` タプルのリスト (en_index 昇順)。
    """
    # ------ パス 1: アンカーベースの対応づけ ------

    # 非 None フィンガープリントを持つブロックのみ抽出 (元のインデックスを保持)
    en_anchors: list[tuple[int, tuple]] = [
        (i, fp)
        for i, b in enumerate(en_blocks)
        if (fp := extract_fingerprint(b)) is not None
    ]
    ja_anchors: list[tuple[int, tuple]] = [
        (i, fp)
        for i, b in enumerate(ja_blocks)
        if (fp := extract_fingerprint(b)) is not None
    ]

    en_fp_seq = [fp for _, fp in en_anchors]
    ja_fp_seq = [fp for _, fp in ja_anchors]

    sm = difflib.SequenceMatcher(None, en_fp_seq, ja_fp_seq, autojunk=False)

    anchor_matches: list[tuple[int, int]] = []
    for match in sm.get_matching_blocks():
        for k in range(match.size):
            en_idx = en_anchors[match.a + k][0]
            ja_idx = ja_anchors[match.b + k][0]
            anchor_matches.append((en_idx, ja_idx))

    # ------ パス 2: ギャップ内の位置照合 ------

    all_matches: list[tuple[int, int]] = []

    # 番兵値を追加してギャップ処理を統一する
    boundaries = (
        [(-1, -1)] + anchor_matches + [(len(en_blocks), len(ja_blocks))]
    )

    for i in range(len(boundaries) - 1):
        prev_en, prev_ja = boundaries[i]
        next_en, next_ja = boundaries[i + 1]

        # アンカーマッチ自体を結果に追加 (番兵はスキップ)
        if i > 0:
            all_matches.append(boundaries[i])

        # ギャップ内のブロックインデックスを列挙
        en_gap = list(range(prev_en + 1, next_en))
        ja_gap = list(range(prev_ja + 1, next_ja))

        # 位置で 1:1 対応づけ (少ない方に合わせる)
        for k in range(min(len(en_gap), len(ja_gap))):
            all_matches.append((en_gap[k], ja_gap[k]))

    # en_index 昇順でソート
    all_matches.sort()

    return all_matches


# ---------------------------------------------------------------------------
# 行番号ルックアップ
# ---------------------------------------------------------------------------

def find_ja_lines_for_block(
    en_block_index: int,
    matches: list[tuple[int, int]],
    ja_blocks: list[Block],
) -> tuple[int, int] | None:
    """EN ブロックインデックスに対応する JA ブロックの行範囲を返す。

    Args:
        en_block_index: 検索対象の EN ブロックインデックス。
        matches: :func:`match_blocks` が返した対応ペアのリスト。
        ja_blocks: JA ブロックのリスト。

    Returns:
        ``(start_line, end_line)`` タプル (1-indexed)、
        対応が見つからない場合は None。
    """
    for en_idx, ja_idx in matches:
        if en_idx == en_block_index:
            if 0 <= ja_idx < len(ja_blocks):
                b = ja_blocks[ja_idx]
                return (b.start_line, b.end_line)
            return None
    return None
