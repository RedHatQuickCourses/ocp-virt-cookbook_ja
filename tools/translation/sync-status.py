#!/usr/bin/env python3
"""翻訳カバレッジダッシュボードスクリプト。

翻訳状況の全体ダッシュボードを表示する。管理対象ファイルのステータスサマリー、
未翻訳ファイルの一覧を出力する。

使い方:
  # フルダッシュボード
  python3 tools/translation/sync-status.py

  # サマリーのみ (ブロック詳細なし)
  python3 tools/translation/sync-status.py --summary

  # 特定ディレクトリに絞って表示
  python3 tools/translation/sync-status.py modules/networking/

  # upstream ref を指定
  python3 tools/translation/sync-status.py --upstream-ref origin/main
"""

import argparse
import re
import sys

from _lib.git_utils import git_ls_tree
from _lib.manifest import load_manifest, MANIFEST_PATH

# Pattern for page files: modules/<module>/pages/*.adoc
_PAGE_FILE_RE = re.compile(r"^modules/[^/]+/pages/.+\.adoc$")
# Pattern for nav.adoc files: modules/<module>/nav.adoc
_NAV_FILE_RE = re.compile(r"^modules/[^/]+/nav\.adoc$")


def _is_page_or_nav(path: str) -> bool:
    """upstream のパスがページファイルまたは nav.adoc かを判定する。"""
    return bool(_PAGE_FILE_RE.match(path) or _NAV_FILE_RE.match(path))


def _matches_scope(path: str, scopes: list[str]) -> bool:
    """パスが指定されたスコープに含まれるかを判定する。

    スコープはプレフィックスマッチで判定する。
    """
    for scope in scopes:
        # Normalize: ensure directory scopes end with /
        if not scope.endswith("/") and not scope.endswith(".adoc"):
            scope = scope + "/"
        if path.startswith(scope) or path == scope:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["modules"],
        help="範囲。デフォルト: modules",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="ファイル単位の集計のみ表示 (ブロック詳細なし)",
    )
    parser.add_argument(
        "--upstream-ref",
        default="upstream/main",
        help="upstream の git ref (デフォルト: upstream/main)",
    )
    args = parser.parse_args()

    # 1. Load manifest (returns empty structure if file doesn't exist)
    manifest = load_manifest()
    managed_files = manifest.get("files", {})

    # 2. List all upstream .adoc files via git ls-tree
    all_upstream = git_ls_tree(args.upstream_ref, "modules/")
    if not all_upstream:
        print(
            f"Warning: no files found in {args.upstream_ref} under modules/",
            file=sys.stderr,
        )

    # 3. Filter to page files and nav.adoc, then apply scope
    upstream_pages = [
        p for p in all_upstream
        if _is_page_or_nav(p) and _matches_scope(p, args.paths)
    ]

    total_upstream = len(upstream_pages)

    # 4. Classify each file
    synced_files: list[str] = []
    changed_files: list[tuple[str, int, int]] = []  # (path, outdated_count, new_count)
    unmanaged_files: list[str] = []

    total_synced_blocks = 0
    total_tracked_blocks = 0

    for upath in sorted(upstream_pages):
        file_entry = managed_files.get(upath)
        if file_entry is None:
            unmanaged_files.append(upath)
            continue

        blocks = file_entry.get("blocks", {})
        num_blocks = len(blocks)
        total_tracked_blocks += num_blocks

        outdated = 0
        new = 0
        synced = 0
        for block_info in blocks.values():
            status = block_info.get("status", "")
            if status == "synced":
                synced += 1
            elif status == "outdated":
                outdated += 1
            elif status == "new":
                new += 1
            # Other statuses (e.g. "removed") count as tracked but not synced

        total_synced_blocks += synced

        if outdated == 0 and new == 0:
            synced_files.append(upath)
        else:
            changed_files.append((upath, outdated, new))

    managed_count = len(synced_files) + len(changed_files)
    unmanaged_count = len(unmanaged_files)

    # 5. Output
    print("=== Translation Status ===")
    print()

    # Managed files summary
    if total_upstream > 0:
        pct = managed_count / total_upstream * 100
        print(
            f"管理対象ファイル:      "
            f"{managed_count:,} / {total_upstream:,} upstream files "
            f"({pct:.1f}%)"
        )
    else:
        print(f"管理対象ファイル:      {managed_count:,} / 0 upstream files")

    print(f"全同期済み:           {len(synced_files):,}")
    print(f"変更あり:              {len(changed_files):,}")

    if changed_files and not args.summary:
        for path, outdated, new in changed_files:
            parts = []
            if outdated > 0:
                parts.append(f"{outdated} outdated")
            if new > 0:
                parts.append(f"{new} new")
            detail = ", ".join(parts)
            print(f"  {path:<55s} {detail}")

    print()

    # Unmanaged files
    print(f"未翻訳 upstream ファイル:  {unmanaged_count:,}")
    if unmanaged_files and not args.summary:
        for path in unmanaged_files:
            print(f"  {path}")

    print()

    # Total block stats
    if total_tracked_blocks > 0:
        block_pct = total_synced_blocks / total_tracked_blocks * 100
        print(
            f"Total blocks: "
            f"{total_synced_blocks:,} synced / "
            f"{total_tracked_blocks:,} tracked "
            f"({block_pct:.1f}%)"
        )
    else:
        print("Total blocks: 0 synced / 0 tracked")


if __name__ == "__main__":
    main()
