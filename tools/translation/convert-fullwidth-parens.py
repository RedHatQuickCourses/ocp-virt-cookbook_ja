#!/usr/bin/env python3
"""全角括弧 (（）) を半角括弧 (()) に変換する。

AsciiDoc (.adoc) ファイルを対象に、全角の（および）を半角の ( および ) に
変換する。コードブロック (---- / ....) 内は変更しない。
"""

import argparse
import glob
import os
import re
import sys


def process_file(filepath: str, *, dry_run: bool = False) -> list[tuple[int, str, str]]:
    """ファイルを処理し、変更箇所のリストを返す。"""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    in_block = False
    block_char = None
    new_lines: list[str] = []
    diffs: list[tuple[int, str, str]] = []

    for lineno, line in enumerate(lines, 1):
        raw = line.rstrip("\n").rstrip("\r")

        if not in_block:
            if re.match(r"^(-{4,}|\.{4,})$", raw):
                in_block = True
                block_char = raw[0]
                new_lines.append(line)
                continue
        else:
            if re.match(rf"^[{re.escape(block_char)}]{{4,}}$", raw):
                in_block = False
                block_char = None
                new_lines.append(line)
                continue
            new_lines.append(line)
            continue

        processed = raw.replace("（", "(").replace("）", ")")
        if processed != raw:
            diffs.append((lineno, raw, processed))
            ending = line[len(raw) :]
            new_lines.append(processed + ending)
        else:
            new_lines.append(line)

    if diffs and not dry_run:
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    return diffs


def _collect_files(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "**", "*.adoc"), recursive=True)))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"Warning: '{p}' is not a valid file or directory", file=sys.stderr)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=["modules"], help="処理対象のファイルまたはディレクトリ (デフォルト: modules/)")
    parser.add_argument("--dry-run", action="store_true", help="変更を適用せず差分のみ表示する")
    args = parser.parse_args()

    files = _collect_files(args.paths)
    if not files:
        print("対象ファイルが見つかりません。", file=sys.stderr)
        sys.exit(1)

    total_changes = 0
    modified_files = 0

    for filepath in files:
        diffs = process_file(filepath, dry_run=args.dry_run)
        if diffs:
            modified_files += 1
            total_changes += len(diffs)
            rel = os.path.relpath(filepath)
            if args.dry_run:
                print(f"\n{rel}: {len(diffs)} changes")
                for lineno, old, new in diffs:
                    print(f"  L{lineno}:")
                    print(f"    - {old}")
                    print(f"    + {new}")
            else:
                print(f"{rel}: {len(diffs)} lines")

    action = "would be modified" if args.dry_run else "modified"
    print(f"\n{modified_files} files {action}, {total_changes} lines changed")


if __name__ == "__main__":
    main()
