#!/usr/bin/env python3
"""翻訳同期マニフェストの初期化スクリプト。

日本語ファイルを翻訳同期管理の対象として登録する。upstream の英語原文を
取得し、スナップショットとして保存し、ブロックハッシュを計算して
マニフェストに登録する。

使い方:
  # 特定ファイルの初期化
  python3 tools/translation/sync-init.py modules/networking/pages/linux-bridges.adoc

  # ディレクトリ内の全 .adoc を初期化
  python3 tools/translation/sync-init.py modules/networking/

  # 全モジュールを初期化 (デフォルト)
  python3 tools/translation/sync-init.py

  # プレビュー
  python3 tools/translation/sync-init.py --dry-run

  # 既存エントリも再初期化
  python3 tools/translation/sync-init.py --force
"""

import argparse
import os
import sys

from _lib.common import _collect_files
from _lib.git_utils import git_show, git_rev_parse, git_remote_exists
from _lib.block_parser import parse_blocks, generate_block_ids, compute_block_hash
from _lib.manifest import load_manifest, save_manifest, add_file_entry, get_file_entry

ORIGINALS_DIR = "tools/translation/originals"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["modules"],
        help="日本語ファイルまたはディレクトリ (デフォルト: modules)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="変更せずに結果を表示",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既にマニフェストに存在するファイルも再初期化",
    )
    parser.add_argument(
        "--upstream-ref",
        default="upstream/main",
        help="upstream の git ref (デフォルト: upstream/main)",
    )
    args = parser.parse_args()

    # Extract remote name from upstream-ref (e.g. "upstream/main" -> "upstream")
    # If the ref is a bare SHA or tag (no "/"), default to "upstream"
    if "/" in args.upstream_ref:
        remote_name = args.upstream_ref.split("/")[0]
    else:
        remote_name = "upstream"

    # Check that the upstream remote exists
    if not git_remote_exists(remote_name):
        print(
            f"Error: remote '{remote_name}' not found. "
            "Run: git remote add upstream "
            "https://github.com/RedHatQuickCourses/ocp-virt-cookbook.git",
            file=sys.stderr,
        )
        sys.exit(1)

    # Verify the upstream ref can be resolved
    try:
        upstream_commit = git_rev_parse(args.upstream_ref)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Collect target files
    files = _collect_files(args.paths)
    if not files:
        print("No .adoc files found.", file=sys.stderr)
        sys.exit(0)

    manifest = load_manifest()

    files_initialized = 0
    total_blocks = 0

    for filepath in files:
        rel_path = filepath

        # Must be under modules/
        if not rel_path.startswith("modules/"):
            print(f"Warning: '{rel_path}' is not under modules/, skipping", file=sys.stderr)
            continue

        # Check if already in manifest
        if get_file_entry(manifest, rel_path) is not None and not args.force:
            print(f"{rel_path}: already in manifest (use --force to re-initialize)", file=sys.stderr)
            continue

        # upstream path is the same relative path
        upstream_path = rel_path

        # Get English original from upstream
        en_content = git_show(args.upstream_ref, upstream_path)
        if en_content is None:
            print(f"Warning: '{upstream_path}' not found in {args.upstream_ref}, skipping", file=sys.stderr)
            continue

        # Parse English content into blocks
        blocks = parse_blocks(en_content)
        generate_block_ids(blocks)

        # Build block data: list of (block_id, block_type, en_hash)
        block_data = []
        for block in blocks:
            en_hash = compute_block_hash(block.lines)
            block_data.append((block.block_id, block.block_type, en_hash))

        num_blocks = len(block_data)

        if not args.dry_run:
            # Save English original snapshot
            original_path = os.path.join(ORIGINALS_DIR, rel_path)
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            with open(original_path, "w", encoding="utf-8") as f:
                f.write(en_content)

            # Add entry to manifest
            add_file_entry(manifest, rel_path, upstream_path, upstream_commit, block_data)

        print(f"{rel_path}: initialized ({num_blocks} blocks)")
        files_initialized += 1
        total_blocks += num_blocks

    # Write manifest
    if not args.dry_run and files_initialized > 0:
        save_manifest(manifest)

    # Final summary
    if files_initialized > 0:
        print()
        print(f"{files_initialized} files initialized, {total_blocks} blocks tracked")


if __name__ == "__main__":
    main()
