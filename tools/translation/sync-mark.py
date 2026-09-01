#!/usr/bin/env python3
"""翻訳反映済みマークスクリプト。

翻訳者が日本語テキストを更新した後、該当ブロックを「反映済み」として
マークする。マニフェストのハッシュとスナップショットを現在の upstream に
更新する。

使い方:
  # ファイル内の全 outdated/new ブロックをマーク
  python3 tools/translation/sync-mark.py modules/networking/pages/linux-bridges.adoc

  # 特定ブロックのみマーク
  python3 tools/translation/sync-mark.py modules/networking/pages/linux-bridges.adoc \\
    --blocks "overview/prose/0" "create-net-attach-def/prose/2"

  # 全管理対象ファイルの全ブロックをマーク
  python3 tools/translation/sync-mark.py --all

  # プレビュー
  python3 tools/translation/sync-mark.py --dry-run
"""

import argparse
import os
import sys

from _lib.common import _collect_files
from _lib.git_utils import git_show, git_rev_parse
from _lib.block_parser import parse_blocks, generate_block_ids, compute_block_hash
from _lib.manifest import (load_manifest, save_manifest, get_file_entry,
                           update_block_status, remove_block_entry, MANIFEST_PATH)

ORIGINALS_DIR = "tools/translation/originals"
UPSTREAM_REF = "upstream/main"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["modules"],
        help="対象ファイルまたはディレクトリ (デフォルト: modules)",
    )
    parser.add_argument(
        "--blocks",
        nargs="+",
        help="マークする特定のブロック ID リスト (省略時は全 outdated/new ブロック)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="全管理対象ファイルを対象にする",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="変更せずに結果を表示",
    )
    args = parser.parse_args()

    # Check manifest exists
    if not os.path.exists(MANIFEST_PATH):
        print("Error: manifest.json not found. Run sync-init.py first.", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()

    # Determine target files
    if args.all:
        target_files = sorted(manifest["files"].keys())
    else:
        collected = _collect_files(args.paths)
        target_files = [f for f in collected if f in manifest["files"]]

    if not target_files:
        print("No managed files found.", file=sys.stderr)
        sys.exit(0)

    # Get current upstream HEAD
    try:
        upstream_commit = git_rev_parse(UPSTREAM_REF)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    total_files_updated = 0
    total_blocks_synced = 0

    for rel_path in target_files:
        file_entry = get_file_entry(manifest, rel_path)
        if file_entry is None:
            continue

        # a. Get current upstream English
        upstream_path = file_entry.get("upstream_path", rel_path)
        en_content = git_show(UPSTREAM_REF, upstream_path)
        if en_content is None:
            print(f"Warning: '{upstream_path}' not found in {UPSTREAM_REF}, skipping",
                  file=sys.stderr)
            continue

        # b. Parse blocks and compute new hashes
        blocks = parse_blocks(en_content)
        generate_block_ids(blocks)

        new_hashes: dict[str, str] = {}
        for block in blocks:
            new_hashes[block.block_id] = compute_block_hash(block.lines)

        # c. Identify blocks to mark
        blocks_in_manifest = file_entry["blocks"]

        if args.blocks:
            # Only mark specified block IDs
            target_block_ids = [
                bid for bid in args.blocks if bid in blocks_in_manifest
            ]
        else:
            # Mark all outdated/new/removed blocks
            target_block_ids = [
                bid for bid, binfo in blocks_in_manifest.items()
                if binfo["status"] in ("outdated", "new", "removed")
            ]

        if not target_block_ids:
            continue

        synced_ids: list[str] = []
        removed_ids: list[str] = []

        for block_id in target_block_ids:
            block_info = blocks_in_manifest.get(block_id)
            if block_info is None:
                continue

            if block_info["status"] == "removed":
                # Remove from manifest
                if not args.dry_run:
                    remove_block_entry(file_entry, block_id)
                removed_ids.append(block_id)
            else:
                # Update to synced
                new_hash = new_hashes.get(block_id)
                if new_hash is None:
                    # Block no longer exists in upstream; skip
                    continue
                if not args.dry_run:
                    update_block_status(file_entry, block_id, "synced", en_hash=new_hash)
                synced_ids.append(block_id)

        marked_count = len(synced_ids) + len(removed_ids)
        if marked_count == 0:
            continue

        # Output
        print(f"{rel_path}: {marked_count} blocks marked synced")
        for bid in synced_ids:
            print(f"  SYNCED  {bid}")
        for bid in removed_ids:
            print(f"  REMOVED  {bid}")

        if not args.dry_run:
            # d. Check if all blocks are now synced, then overwrite snapshot
            all_synced = all(
                b["status"] == "synced"
                for b in file_entry["blocks"].values()
            )
            if all_synced:
                original_path = os.path.join(ORIGINALS_DIR, rel_path)
                os.makedirs(os.path.dirname(original_path), exist_ok=True)
                with open(original_path, "w", encoding="utf-8") as f:
                    f.write(en_content)

            # e. Update upstream_commit
            file_entry["upstream_commit"] = upstream_commit

        total_files_updated += 1
        total_blocks_synced += marked_count

    # Write manifest
    if not args.dry_run and total_files_updated > 0:
        save_manifest(manifest)

    # Final summary
    if total_files_updated > 0:
        print()
        print(f"{total_files_updated} file{'s' if total_files_updated != 1 else ''} updated, "
              f"{total_blocks_synced} blocks synced")


if __name__ == "__main__":
    main()
