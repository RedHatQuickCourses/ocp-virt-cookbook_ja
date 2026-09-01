#!/usr/bin/env python3
"""構造一致バリデーションスクリプト。

英語スナップショットと日本語ファイルのブロック構造が 1:1 で対応していることを
検証する。運用ルール (段落の統合・分割・並べ替え禁止) の違反を検出する。

使い方:
  # 全管理対象ファイルを検証
  python3 tools/translation/validate-structure.py

  # 特定ファイルのみ
  python3 tools/translation/validate-structure.py modules/networking/pages/linux-bridges.adoc

  # CI用: 違反があれば非ゼロ終了
  python3 tools/translation/validate-structure.py --strict
"""

import argparse
import os
import re
import sys
from collections import defaultdict

from _lib.common import _collect_files
from _lib.block_parser import parse_blocks, generate_block_ids, compute_block_hash
from _lib.manifest import load_manifest, MANIFEST_PATH

ORIGINALS_DIR = "tools/translation/originals"


def _files_in_scope(manifest: dict, paths: list[str]) -> list[str]:
    """マニフェスト内のファイルを指定パスでフィルタリングする。

    Args:
        manifest: マニフェスト辞書。
        paths: フィルタ対象のパスリスト。

    Returns:
        マニフェスト内で指定パスに該当するファイルパスのソート済みリスト。
    """
    all_files = sorted(manifest.get("files", {}).keys())
    if not paths:
        return all_files

    result = []
    for f in all_files:
        for p in paths:
            if f == p or f.startswith(p.rstrip("/") + "/"):
                result.append(f)
                break
    return result


def _section_level(line: str) -> int:
    """セクションヘッダー行の階層レベルを返す。

    ``== Title`` -> 2, ``=== Title`` -> 3, etc.
    """
    m = re.match(r"^(={2,5})\s", line)
    if m:
        return len(m.group(1))
    return 0


def _group_by_section(blocks: list) -> dict[str, list]:
    """ブロックリストをセクションパスごとにグルーピングする。

    Returns:
        セクションパスをキー、ブロックリストを値とする辞書。
    """
    groups: dict[str, list] = defaultdict(list)
    for block in blocks:
        groups[block.section_path].append(block)
    return dict(groups)


def _count_types(blocks: list) -> dict[str, int]:
    """ブロックリスト内のブロック種別ごとの出現回数を返す。"""
    counts: dict[str, int] = defaultdict(int)
    for block in blocks:
        counts[block.block_type] += 1
    return dict(counts)


def _split_by_section_order(blocks: list) -> list[tuple[str, list]]:
    """ブロックリストをセクション境界で分割し、出現順のリストで返す。

    Returns:
        ``(section_label, blocks_in_section)`` のリスト (出現順)。
    """
    sections: list[tuple[str, list]] = []
    current_label = "_root"
    current_blocks: list = []
    for b in blocks:
        if b.block_type == "section_header":
            if current_blocks:
                sections.append((current_label, current_blocks))
            current_label = b.section_path
            current_blocks = [b]
        else:
            current_blocks.append(b)
    if current_blocks:
        sections.append((current_label, current_blocks))
    return sections


def _validate_file(en_content: str, ja_content: str, rel_path: str) -> list[str]:
    """1ファイルの構造一致を検証し、違反メッセージのリストを返す。

    EN と JA のセクションはスラッグが異なる (英語 vs 日本語) ため、
    セクション名ではなく **出現順** で1:1に対応づけて比較する。

    Args:
        en_content: 英語スナップショットのテキスト。
        ja_content: 日本語ファイルのテキスト。
        rel_path: 表示用の相対パス。

    Returns:
        違反メッセージのリスト (空なら OK)。
    """
    en_blocks = parse_blocks(en_content)
    generate_block_ids(en_blocks)
    ja_blocks = parse_blocks(ja_content)
    generate_block_ids(ja_blocks)

    violations: list[str] = []

    # --- (e) セクション見出しの順序と階層レベルが一致するか検証 ---
    en_headers = [b for b in en_blocks if b.block_type == "section_header"]
    ja_headers = [b for b in ja_blocks if b.block_type == "section_header"]

    if len(en_headers) != len(ja_headers):
        violations.append(
            f"  WARNING  section heading count: "
            f"EN has {len(en_headers)}, JA has {len(ja_headers)}"
        )
    else:
        for idx, (en_h, ja_h) in enumerate(zip(en_headers, ja_headers)):
            en_level = _section_level(en_h.lines[0]) if en_h.lines else 0
            ja_level = _section_level(ja_h.lines[0]) if ja_h.lines else 0
            if en_level != ja_level:
                violations.append(
                    f"  WARNING  section heading #{idx + 1}: "
                    f"EN level {en_level}, JA level {ja_level}\n"
                    f"           (ja: L{ja_h.start_line})"
                )

    # --- (d) セクションごとにブロック種別の数を比較 ---
    en_secs = _split_by_section_order(en_blocks)
    ja_secs = _split_by_section_order(ja_blocks)

    for idx in range(min(len(en_secs), len(ja_secs))):
        en_label, en_group = en_secs[idx]
        ja_label, ja_group = ja_secs[idx]
        display_label = ja_label if ja_label != "_root" else en_label

        en_counts = _count_types(en_group)
        ja_counts = _count_types(ja_group)

        all_types = sorted(set(list(en_counts.keys()) + list(ja_counts.keys())))
        for btype in all_types:
            if btype == "section_header":
                continue
            en_n = en_counts.get(btype, 0)
            ja_n = ja_counts.get(btype, 0)
            if en_n != ja_n:
                ja_type_blocks = [b for b in ja_group if b.block_type == btype]
                if ja_type_blocks:
                    ja_start = ja_type_blocks[0].start_line
                    ja_end = ja_type_blocks[-1].end_line
                    line_info = f"L{ja_start}-{ja_end}"
                else:
                    line_info = "N/A"

                hint = ""
                if ja_n > en_n:
                    hint = " -- paragraph may have been split"
                elif ja_n < en_n:
                    hint = " -- paragraph may have been merged"

                violations.append(
                    f'  WARNING  section "{display_label}": '
                    f"EN has {en_n} {btype}, JA has {ja_n}\n"
                    f"           (ja: {line_info}{hint})"
                )

    # --- (f) コードブロックの内容が同一か検証 (コメント行は翻訳対象のため除外) ---
    for idx in range(min(len(en_secs), len(ja_secs))):
        en_label, en_group = en_secs[idx]
        ja_label, ja_group = ja_secs[idx]
        display_label = ja_label if ja_label != "_root" else en_label

        en_code = [b for b in en_group if b.block_type == "code_block"]
        ja_code = [b for b in ja_group if b.block_type == "code_block"]

        pairs = min(len(en_code), len(ja_code))
        for i in range(pairs):
            en_non_comment = [l for l in en_code[i].lines
                              if not l.lstrip().startswith("#")]
            ja_non_comment = [l for l in ja_code[i].lines
                              if not l.lstrip().startswith("#")]
            en_hash = compute_block_hash(en_non_comment)
            ja_hash = compute_block_hash(ja_non_comment)
            if en_hash != ja_hash:
                violations.append(
                    f'  WARNING  section "{display_label}": '
                    f"code_block/{i} content differs\n"
                    f"           (ja: L{ja_code[i].start_line}"
                    f" -- code block may have been modified)"
                )

    return violations


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["modules"],
        help="検証対象のファイルまたはディレクトリ (デフォルト: modules)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="違反があれば終了コード 1 で終了 (CI 用)",
    )
    args = parser.parse_args()

    # マニフェストの存在チェック
    if not os.path.exists(MANIFEST_PATH):
        print(
            f"Error: {MANIFEST_PATH} not found. "
            "Run sync-init.py first to initialize the manifest.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = load_manifest()

    if not manifest.get("files"):
        print(
            "Error: manifest contains no file entries. "
            "Run sync-init.py first to register files.",
            file=sys.stderr,
        )
        sys.exit(1)

    # マニフェスト内のファイルを指定パスでフィルタリング
    target_files = _files_in_scope(manifest, args.paths)

    if not target_files:
        print("No managed files found for the specified paths.", file=sys.stderr)
        sys.exit(0)

    files_ok = 0
    files_with_violations = 0

    for rel_path in target_files:
        # スナップショットファイルの読み込み
        snapshot_path = os.path.join(ORIGINALS_DIR, rel_path)
        if not os.path.exists(snapshot_path):
            print(
                f"Warning: snapshot not found for '{rel_path}', skipping",
                file=sys.stderr,
            )
            continue

        with open(snapshot_path, encoding="utf-8") as f:
            en_content = f.read()

        # 日本語ファイルの読み込み
        if not os.path.exists(rel_path):
            print(
                f"Warning: JA file not found: '{rel_path}', skipping",
                file=sys.stderr,
            )
            continue

        with open(rel_path, encoding="utf-8") as f:
            ja_content = f.read()

        # 構造検証
        violations = _validate_file(en_content, ja_content, rel_path)

        # ブロック数の計算 (JA 側)
        ja_blocks = parse_blocks(ja_content)
        block_count = len(ja_blocks)

        if violations:
            files_with_violations += 1
            print(f"{rel_path}: {len(violations)} violations")
            for v in violations:
                print(v)
            print()
        else:
            files_ok += 1
            print(f"{rel_path}: OK ({block_count} blocks)")
            print()

    # サマリー出力
    parts = []
    if files_with_violations > 0:
        parts.append(
            f"{files_with_violations} file{'s' if files_with_violations != 1 else ''} "
            f"with violations"
        )
    if files_ok > 0:
        parts.append(
            f"{files_ok} file{'s' if files_ok != 1 else ''} OK"
        )
    if parts:
        print(", ".join(parts))

    # --strict: 違反があれば非ゼロ終了
    if args.strict and files_with_violations > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
