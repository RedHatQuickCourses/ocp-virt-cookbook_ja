#!/usr/bin/env python3
"""upstream の変更を検知し、翻訳が必要なブロックを報告する。

スナップショットと upstream の現在の英語をブロック単位で比較し、
変更があったブロックを不変アンカー照合で日本語ファイルの行番号に
マッピングして報告する。

使い方:
  # 全ファイルをチェック
  python3 tools/translation/sync-check.py

  # 特定のモジュールのみ
  python3 tools/translation/sync-check.py modules/networking/

  # upstream を先にフェッチしてからチェック
  python3 tools/translation/sync-check.py --fetch

  # ブロック単位の英語 diff を表示
  python3 tools/translation/sync-check.py --verbose
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

from _lib.anchor_matching import find_ja_lines_for_block, match_blocks
from _lib.block_parser import compute_block_hash, generate_block_ids, parse_blocks
from _lib.common import _collect_files
from _lib.git_utils import git_fetch, git_ls_tree, git_rev_parse, git_show
from _lib.manifest import (
    MANIFEST_PATH,
    get_file_entry,
    load_manifest,
    save_manifest,
    update_block_status,
)


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def _matches_paths(rel_path: str, paths: list[str]) -> bool:
    """マニフェストのファイルパスが指定パスに該当するか判定する。

    ``rel_path.startswith(path)`` で各パスをチェックし、
    特定のファイルパスとの完全一致も許容する。
    """
    for p in paths:
        if rel_path.startswith(p):
            return True
        # 末尾スラッシュなしでもディレクトリとして照合
        p_norm = p.rstrip("/")
        if rel_path == p_norm or rel_path.startswith(p_norm + "/"):
            return True
    return False


def _format_ja_lines(ja_range: tuple[int, int] | None) -> str:
    """JA 行番号範囲をフォーマットする。"""
    if ja_range is None:
        return "(ja: mapping not available)"
    start, end = ja_range
    if start == end:
        return f"(ja: L{start})"
    return f"(ja: L{start}-{end})"


def _plural(n: int, singular: str, plural_form: str) -> str:
    """数値に応じた単数/複数形の文字列を返す。"""
    return f"{n} {singular}" if n == 1 else f"{n} {plural_form}"


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["modules"],
        help="チェック範囲 (デフォルト: modules)",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="チェック前に git fetch upstream を実行",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="変更ブロックの英語 diff を表示",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="マニフェストのステータスを更新しない (レポートのみ)",
    )
    parser.add_argument(
        "--upstream-ref",
        default="upstream/main",
        help="upstream の Git 参照 (デフォルト: upstream/main)",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. マニフェスト読み込み
    # ------------------------------------------------------------------
    if not os.path.exists(MANIFEST_PATH):
        print(
            "Error: manifest.json not found. Run sync-init.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = load_manifest()

    # --fetch が指定された場合、upstream をフェッチ
    if args.fetch:
        remote = args.upstream_ref.split("/")[0] if "/" in args.upstream_ref else "upstream"
        print(f"Fetching {remote}...")
        git_fetch(remote)

    upstream_ref = args.upstream_ref

    # ------------------------------------------------------------------
    # 2. 管理対象ファイルを処理
    # ------------------------------------------------------------------
    files_with_changes = 0
    files_synced = 0
    total_outdated = 0
    total_new = 0
    total_removed = 0

    # upstream_path の集合 (管理対象外ファイル検出用)
    managed_upstream_paths: set[str] = set()

    first_file = True

    for rel_path in sorted(manifest["files"].keys()):
        if not _matches_paths(rel_path, args.paths):
            continue

        file_entry = manifest["files"][rel_path]
        upstream_path = file_entry.get("upstream_path", rel_path)
        managed_upstream_paths.add(upstream_path)

        # 2a. スナップショット読み込み
        snapshot_path = os.path.join(
            "tools", "translation", "originals", rel_path
        )
        if not os.path.exists(snapshot_path):
            if not first_file:
                print()
            print(f"{rel_path}: snapshot not found (skipped)")
            first_file = False
            continue

        with open(snapshot_path, encoding="utf-8") as f:
            snapshot_content = f.read()

        # 2b. upstream の現在の英語を取得
        upstream_content = git_show(upstream_ref, upstream_path)

        # 2c. upstream にファイルが存在しない場合
        if upstream_content is None:
            if not first_file:
                print()
            print(f"{rel_path}: deleted in upstream")
            if not args.dry_run:
                for block_id in list(file_entry.get("blocks", {}).keys()):
                    update_block_status(file_entry, block_id, "removed")
            files_with_changes += 1
            total_removed += len(file_entry.get("blocks", {}))
            first_file = False
            continue

        # 2d. 両方をブロック解析
        snapshot_blocks = parse_blocks(snapshot_content)
        generate_block_ids(snapshot_blocks)
        upstream_blocks = parse_blocks(upstream_content)
        generate_block_ids(upstream_blocks)

        # 2e. ブロック ID で辞書を構築
        snapshot_by_id: dict[str, int] = {}
        for i, b in enumerate(snapshot_blocks):
            snapshot_by_id[b.block_id] = i

        upstream_by_id: dict[str, int] = {}
        for i, b in enumerate(upstream_blocks):
            upstream_by_id[b.block_id] = i

        manifest_blocks = file_entry.get("blocks", {})

        # 2f-2i. ハッシュ比較と変更検知
        file_outdated: list[str] = []
        file_new: list[str] = []
        file_removed: list[str] = []

        for block_id in sorted(manifest_blocks.keys()):
            block_info = manifest_blocks[block_id]
            if block_id in upstream_by_id:
                idx = upstream_by_id[block_id]
                upstream_hash = compute_block_hash(
                    upstream_blocks[idx].lines
                )
                if upstream_hash != block_info["en_hash"]:
                    file_outdated.append(block_id)
            else:
                file_removed.append(block_id)

        for block_id in upstream_by_id:
            if block_id not in manifest_blocks:
                file_new.append(block_id)
        file_new.sort()

        # ------------------------------------------------------------------
        # 出力
        # ------------------------------------------------------------------
        if not first_file:
            print()
        first_file = False

        if file_outdated or file_new or file_removed:
            # ファイルサマリー行
            parts: list[str] = []
            if file_outdated:
                parts.append(f"{len(file_outdated)} outdated")
            if file_new:
                parts.append(f"{len(file_new)} new")
            if file_removed:
                parts.append(f"{len(file_removed)} removed")
            print(f"{rel_path}: {', '.join(parts)}")

            # 3. 不変アンカー照合で JA 行番号を特定
            ja_blocks_list = None
            matches_list = None

            if os.path.exists(rel_path):
                with open(rel_path, encoding="utf-8") as f:
                    ja_content = f.read()
                ja_blocks_list = parse_blocks(ja_content)
                generate_block_ids(ja_blocks_list)
                matches_list = match_blocks(snapshot_blocks, ja_blocks_list)

            # OUTDATED ブロック
            for block_id in file_outdated:
                ja_range = None
                en_idx = snapshot_by_id.get(block_id)
                if (
                    en_idx is not None
                    and matches_list is not None
                    and ja_blocks_list is not None
                ):
                    ja_range = find_ja_lines_for_block(
                        en_idx, matches_list, ja_blocks_list
                    )
                print(
                    f"  OUTDATED  {block_id:<40s}"
                    f" {_format_ja_lines(ja_range)}"
                )

                # --verbose: unified diff 表示
                if args.verbose:
                    snap_idx = snapshot_by_id.get(block_id)
                    up_idx = upstream_by_id.get(block_id)
                    if snap_idx is not None and up_idx is not None:
                        snap_lines = [
                            ln + "\n"
                            for ln in snapshot_blocks[snap_idx].lines
                        ]
                        up_lines = [
                            ln + "\n"
                            for ln in upstream_blocks[up_idx].lines
                        ]
                        diff = difflib.unified_diff(
                            snap_lines,
                            up_lines,
                            fromfile="snapshot",
                            tofile="upstream",
                        )
                        for diff_line in diff:
                            print(f"    {diff_line}", end="")
                        # diff 末尾に改行がない場合に備える
                        if up_lines and not up_lines[-1].endswith("\n"):
                            print()

            # NEW ブロック
            for block_id in file_new:
                print(
                    f"  NEW       {block_id:<40s}"
                    f" (ja: mapping not available)"
                )

            # REMOVED ブロック
            for block_id in file_removed:
                ja_range = None
                en_idx = snapshot_by_id.get(block_id)
                if (
                    en_idx is not None
                    and matches_list is not None
                    and ja_blocks_list is not None
                ):
                    ja_range = find_ja_lines_for_block(
                        en_idx, matches_list, ja_blocks_list
                    )
                print(
                    f"  REMOVED   {block_id:<40s}"
                    f" {_format_ja_lines(ja_range)}"
                )

            files_with_changes += 1
            total_outdated += len(file_outdated)
            total_new += len(file_new)
            total_removed += len(file_removed)

            # 5. マニフェスト更新 (--dry-run でなければ)
            if not args.dry_run:
                for block_id in file_outdated:
                    update_block_status(file_entry, block_id, "outdated")
                for block_id in file_removed:
                    update_block_status(file_entry, block_id, "removed")
                for block_id in file_new:
                    up_idx = upstream_by_id[block_id]
                    up_block = upstream_blocks[up_idx]
                    upstream_hash = compute_block_hash(up_block.lines)
                    file_entry["blocks"][block_id] = {
                        "type": up_block.block_type,
                        "en_hash": upstream_hash,
                        "status": "new",
                        "synced_at": None,
                    }
        else:
            print(f"{rel_path}: synced")
            files_synced += 1

    # ------------------------------------------------------------------
    # 4. 管理対象外の upstream ファイルを報告
    # ------------------------------------------------------------------
    upstream_files = git_ls_tree(upstream_ref)
    unmanaged: list[str] = []
    for f in upstream_files:
        if not f.endswith(".adoc"):
            continue
        # pages/ 内のファイルまたは nav.adoc のみ対象
        parts = f.split("/")
        is_page = "pages" in parts
        is_nav = f.endswith("/nav.adoc") or f == "nav.adoc"
        if not is_page and not is_nav:
            continue
        if f not in managed_upstream_paths:
            if _matches_paths(f, args.paths):
                unmanaged.append(f)

    if unmanaged:
        print()
        print("--- Unmanaged upstream files (not translated) ---")
        for f in sorted(unmanaged):
            print(f"  {f}")

    # ------------------------------------------------------------------
    # 最終サマリー
    # ------------------------------------------------------------------
    print()
    summary_parts: list[str] = []
    if files_with_changes:
        summary_parts.append(
            _plural(
                files_with_changes,
                "file with changes",
                "files with changes",
            )
        )
    if files_synced:
        summary_parts.append(
            _plural(files_synced, "file synced", "files synced")
        )
    if not summary_parts:
        summary_parts.append("0 files checked")
    print(", ".join(summary_parts))

    block_parts: list[str] = []
    if total_outdated:
        block_parts.append(
            _plural(total_outdated, "block outdated", "blocks outdated")
        )
    if total_new:
        block_parts.append(
            _plural(total_new, "block new", "blocks new")
        )
    if total_removed:
        block_parts.append(
            _plural(total_removed, "block removed", "blocks removed")
        )
    if block_parts:
        print(", ".join(block_parts))

    # マニフェスト保存
    if not args.dry_run and (total_outdated or total_new or total_removed):
        save_manifest(manifest)


if __name__ == "__main__":
    main()
