#!/usr/bin/env python3
"""翻訳同期ツール共通ユーティリティモジュール。

ファイル収集やその他の共有ヘルパー関数を提供する。
"""

from __future__ import annotations

import glob
import os
import sys


def _collect_files(paths: list[str]) -> list[str]:
    """指定されたパスから .adoc ファイルを収集する。

    各パスについて:
    - ディレクトリの場合: ``**/*.adoc`` を再帰的に検索しソートして追加
    - ファイルの場合: そのまま追加
    - それ以外: 標準エラー出力に警告を表示

    Returns:
        収集されたファイルパスのリスト。
    """
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "**", "*.adoc"), recursive=True)))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"Warning: '{p}' is not a valid file or directory", file=sys.stderr)
    return files
