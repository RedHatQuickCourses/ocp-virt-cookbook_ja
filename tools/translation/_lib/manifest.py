#!/usr/bin/env python3
"""翻訳同期マニフェスト JSON 読み書きモジュール。

マニフェストファイル (manifest.json) の読み込み・書き出し・エントリ操作を提供する。
マニフェストはブロック単位の翻訳状況を管理する唯一の情報源である。
"""

from __future__ import annotations

import datetime
import json
import os

MANIFEST_PATH = "tools/translation/manifest.json"


def _utc_now_iso() -> str:
    """現在の UTC タイムスタンプを ISO 8601 形式で返す。

    末尾は ``+00:00`` ではなく ``Z`` とする。
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_manifest() -> dict:
    """マニフェストをディスクから読み込む。

    ファイルが存在しない場合は空の初期マニフェストを返す。

    Returns:
        マニフェスト辞書。
    """
    if not os.path.exists(MANIFEST_PATH):
        return {
            "version": 1,
            "upstream_remote": "upstream",
            "upstream_branch": "main",
            "files": {},
        }
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(data: dict) -> None:
    """マニフェストをディスクに書き出す。

    親ディレクトリが存在しない場合は作成する。
    JSON はキーをソートし、2 スペースインデントで整形する。
    ファイル末尾に改行を付与する。
    """
    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")


def add_file_entry(
    data: dict,
    rel_path: str,
    upstream_path: str,
    upstream_commit: str,
    blocks: list[tuple[str, str, str]],
) -> None:
    """マニフェストにファイルエントリを追加する。

    Args:
        data: マニフェスト辞書。
        rel_path: 日本語ファイルの相対パス (キー)。
        upstream_path: upstream 側の相対パス。
        upstream_commit: upstream の Git コミット SHA。
        blocks: ``(block_id, block_type, en_hash)`` タプルのリスト。
            全ブロックは ``status: "synced"`` で登録される。
    """
    now = _utc_now_iso()
    blocks_dict: dict[str, dict] = {}
    for block_id, block_type, en_hash in blocks:
        blocks_dict[block_id] = {
            "type": block_type,
            "en_hash": en_hash,
            "status": "synced",
            "synced_at": now,
        }
    data["files"][rel_path] = {
        "upstream_path": upstream_path,
        "upstream_commit": upstream_commit,
        "initialized_at": now,
        "blocks": blocks_dict,
    }


def get_file_entry(data: dict, rel_path: str) -> dict | None:
    """マニフェストからファイルエントリを取得する。

    Args:
        data: マニフェスト辞書。
        rel_path: 日本語ファイルの相対パス。

    Returns:
        ファイルエントリ辞書。未登録の場合は ``None``。
    """
    return data["files"].get(rel_path)


def update_block_status(
    file_entry: dict,
    block_id: str,
    status: str,
    en_hash: str | None = None,
) -> None:
    """ブロックのステータスを更新する。

    ``status`` が ``"synced"`` の場合は ``synced_at`` も現在時刻に更新する。
    ``en_hash`` が指定された場合はハッシュも更新する。

    Args:
        file_entry: ファイルエントリ辞書。
        block_id: 更新対象のブロック ID。
        status: 新しいステータス値。
        en_hash: 新しい英語原文ハッシュ (省略時は更新しない)。
    """
    block = file_entry["blocks"][block_id]
    block["status"] = status
    if en_hash is not None:
        block["en_hash"] = en_hash
    if status == "synced":
        block["synced_at"] = _utc_now_iso()


def remove_file_entry(data: dict, rel_path: str) -> None:
    """マニフェストからファイルエントリを削除する。

    Args:
        data: マニフェスト辞書。
        rel_path: 削除するファイルの相対パス。
    """
    data["files"].pop(rel_path, None)


def remove_block_entry(file_entry: dict, block_id: str) -> None:
    """ファイルエントリからブロックを削除する。

    Args:
        file_entry: ファイルエントリ辞書。
        block_id: 削除するブロック ID。
    """
    file_entry["blocks"].pop(block_id, None)
