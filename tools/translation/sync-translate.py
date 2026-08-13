#!/usr/bin/env python3
"""AI による自動翻訳スクリプト。

sync-check.py で検知された outdated / new ブロックを AI API (Gemini、Claude、
または LiteLLM) で自動翻訳し、日本語ファイルに直接適用する。翻訳作業は main
ブランチから作成した新規ローカルブランチ上で行い、人間がレビューできる状態で
停止する (git add / git commit / git push は行わない)。

使い方:
  # 全管理対象ファイルの outdated/new ブロックを翻訳 (デフォルト: Gemini)
  python3 tools/translation/sync-translate.py

  # Claude API を使用
  python3 tools/translation/sync-translate.py --provider claude

  # LiteLLM を使用 (任意のモデルを指定可能)
  python3 tools/translation/sync-translate.py --provider litellm --model qwen/qwen3-235b-a22b

  # dry-run (翻訳結果を表示するが適用しない、ブランチも作成しない)
  python3 tools/translation/sync-translate.py --dry-run

  # 既存の書式整形ツールも自動実行
  python3 tools/translation/sync-translate.py --format

  # ブランチ名を指定
  python3 tools/translation/sync-translate.py --branch translate/2026-08-12
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from _lib.anchor_matching import extract_fingerprint, match_blocks
from _lib.block_parser import (
    Block,
    compute_block_hash,
    generate_block_ids,
    parse_blocks,
)
from _lib.git_utils import (
    git_branch_exists,
    git_current_branch,
    git_fetch,
    git_ls_tree,
    git_rev_parse,
    git_show,
    git_show_binary,
    git_remote_exists,
    git_status_clean,
    git_switch_create_branch,
)
from _lib.manifest import (
    MANIFEST_PATH,
    add_file_entry,
    get_file_entry,
    load_manifest,
    remove_block_entry,
    remove_file_entry,
    save_manifest,
    update_block_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORIGINALS_DIR = "tools/translation/originals"
UPSTREAM_REF = "upstream/main"
_IMMUTABLE_TYPES = frozenset({"code_block", "literal_block", "block_attribute"})
_NO_TRAILING_BLANK = frozenset({"block_attribute", "block_title"})

# ---------------------------------------------------------------------------
# AI Provider — setup and call
# ---------------------------------------------------------------------------


def _validate_provider(
    provider: str, model: str | None, api_base: str | None = None
) -> tuple[str, object, str, str | None]:
    """Validate provider setup and return ``(provider_name, client, model, api_base)``."""
    if provider == "gemini":
        try:
            from google import genai  # type: ignore[import-untyped]
        except ImportError:
            print(
                "Error: google-genai package not found. "
                "Run: pip install google-genai",
                file=sys.stderr,
            )
            sys.exit(1)
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print(
                "Error: GEMINI_API_KEY environment variable not set",
                file=sys.stderr,
            )
            sys.exit(1)
        client = genai.Client(api_key=api_key)
        return ("gemini", client, model or "gemini-2.5-pro", None)

    if provider == "claude":
        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError:
            print(
                "Error: anthropic package not found. "
                "Run: pip install anthropic",
                file=sys.stderr,
            )
            sys.exit(1)
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print(
                "Error: ANTHROPIC_API_KEY environment variable not set",
                file=sys.stderr,
            )
            sys.exit(1)
        client = anthropic.Anthropic(api_key=api_key)
        return ("claude", client, model or "claude-sonnet-5", None)

    # litellm
    if not model:
        print(
            "Error: --model is required when using --provider litellm",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        import litellm as _litellm  # type: ignore[import-untyped]
    except ImportError:
        print(
            "Error: litellm package not found. Run: pip install litellm",
            file=sys.stderr,
        )
        sys.exit(1)
    return ("litellm", _litellm, model, api_base)


def _call_ai(
    provider_info: tuple[str, object, str, str | None], prompt: str
) -> str | None:
    """Call the AI provider with retry (max 3, exponential backoff).

    Returns translated text, or ``None`` on failure.
    """
    provider_name, client, model, api_base = provider_info
    for attempt in range(3):
        try:
            if provider_name == "gemini":
                resp = client.models.generate_content(
                    model=model, contents=prompt
                )
                return resp.text
            if provider_name == "claude":
                msg = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                return msg.content[0].text
            # litellm
            kwargs: dict = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192,
                "timeout": 120,
            }
            if api_base:
                kwargs["api_base"] = api_base
                kwargs["api_key"] = os.environ.get(
                    "LITELLM_API_KEY",
                    os.environ.get("OPENAI_API_KEY",
                                   os.environ.get("DASHSCOPE_API_KEY", "")),
                )
            resp = client.completion(**kwargs)
            return resp.choices[0].message.content
        except Exception as exc:  # noqa: BLE001
            err = str(exc).lower()
            retryable = ("rate", "429", "quota", "resource",
                         "connection", "timeout", "503", "500",
                         "overloaded", "internal")
            if any(k in err for k in retryable):
                wait = 2 ** (attempt + 1)
                print(
                    f"    Retryable error, retrying in {wait}s... ({type(exc).__name__})",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(f"    API error: {exc}", file=sys.stderr)
                return None
    print("    Failed after 3 retries", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_outdated_prompt(
    section_title: str,
    old_en: str,
    new_en: str,
    current_ja: str,
) -> str:
    return (
        "あなたは技術文書の翻訳者です。OpenShift Virtualization に関する"
        "英語ドキュメントの日本語翻訳を更新してください。\n\n"
        "## ルール\n"
        "- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、"
        "リンク等) はそのまま維持すること\n"
        "- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は"
        "英語のまま残すこと\n"
        "- 既存の日本語翻訳のスタイルと文体を維持すること\n"
        "- 翻訳文のみを出力し、説明や注記は付けないこと\n\n"
        f"## セクション: {section_title}\n\n"
        f"## 旧英語 (翻訳元):\n{old_en}\n\n"
        f"## 新英語 (更新後):\n{new_en}\n\n"
        f"## 現在の日本語翻訳:\n{current_ja}\n\n"
        "## 指示:\n"
        "旧英語から新英語への変更点を、現在の日本語翻訳に反映してください。"
        "変更がない部分はそのまま維持してください。"
    )


def _build_new_prompt(
    section_title: str,
    new_en: str,
    prev_ja: str,
    next_ja: str,
) -> str:
    return (
        "あなたは技術文書の翻訳者です。OpenShift Virtualization に関する"
        "英語ドキュメントを日本語に翻訳してください。\n\n"
        "## ルール\n"
        "- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、"
        "リンク等) はそのまま維持すること\n"
        "- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は"
        "英語のまま残すこと\n"
        "- 以下の前後のブロックの文体に合わせること\n"
        "- 翻訳文のみを出力し、説明や注記は付けないこと\n\n"
        f"## セクション: {section_title}\n\n"
        f"## 前のブロック (文体参考):\n{prev_ja}\n\n"
        f"## 翻訳対象の英語:\n{new_en}\n\n"
        f"## 後のブロック (文体参考):\n{next_ja}"
    )


def _build_new_file_prompt(en_text: str) -> str:
    return (
        "あなたは技術文書の翻訳者です。OpenShift Virtualization に関する"
        "英語ドキュメントを日本語に翻訳してください。\n\n"
        "## ルール\n"
        "- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、"
        "リンク等) はそのまま維持すること\n"
        "- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は"
        "英語のまま残すこと\n"
        "- 翻訳文のみを出力し、説明や注記は付けないこと\n"
        "- 自然な日本語で、技術的に正確な翻訳を行うこと\n\n"
        f"## 翻訳対象の英語:\n{en_text}"
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _get_section_title(blocks: list[Block], target: Block) -> str:
    """Return the human-readable heading for *target*'s section."""
    sp = target.section_path
    if sp == "_root":
        return "(document root)"
    for b in blocks:
        if b.block_type == "section_header" and b.section_path == sp:
            return re.sub(r"^=+\s*", "", b.lines[0]).strip() if b.lines else sp
    return sp


def _find_adjacent_ja(
    upstream_blocks: list[Block],
    up_idx: int,
    direction: int,
    snap_id_to_idx: dict[str, int],
    match_map: dict[int, int],
    ja_blocks: list[Block],
) -> str:
    """Find the nearest translated JA block in *direction* for style ref."""
    idx = up_idx + direction
    while 0 <= idx < len(upstream_blocks):
        sid = snap_id_to_idx.get(upstream_blocks[idx].block_id)
        if sid is not None:
            jid = match_map.get(sid)
            if jid is not None and 0 <= jid < len(ja_blocks):
                return "\n".join(ja_blocks[jid].lines)
        idx += direction
    return "(なし)"


def _reconstruct_file(
    entries: list[tuple[str, list[str]]],
) -> str:
    """Rebuild file text from ``(block_type, lines)`` pairs."""
    parts: list[str] = []
    for i, (btype, lines) in enumerate(entries):
        if i > 0:
            prev_type = entries[i - 1][0]
            need_blank = True
            if prev_type in _NO_TRAILING_BLANK:
                need_blank = False
            elif prev_type == "list_item" and btype == "list_item":
                need_blank = False
            if need_blank:
                parts.append("")
        parts.extend(lines)
    return "\n".join(parts) + "\n" if parts else ""


_RE_XREF_TARGET = re.compile(r"xref:([^\[]+)\[")
_RE_XREF_DISPLAY = re.compile(r"\[([^\]]*)\]")


def _extract_xref_target(line: str) -> str | None:
    m = _RE_XREF_TARGET.search(line)
    return m.group(1) if m else None


def _extract_xref_display(line: str) -> str:
    m = _RE_XREF_DISPLAY.search(line)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Inline sync-check
# ---------------------------------------------------------------------------


def _sync_check_inline(manifest: dict, upstream_ref: str) -> int:
    """Fetch upstream, compare manifest with upstream, update statuses.

    Returns the number of files with changes.
    """
    remote = upstream_ref.split("/")[0]
    git_fetch(remote)

    files_with_changes = 0

    for rel_path in sorted(manifest["files"]):
        entry = manifest["files"][rel_path]
        upstream_path = entry.get("upstream_path", rel_path)

        snap_file = os.path.join(ORIGINALS_DIR, rel_path)
        if not os.path.exists(snap_file):
            continue
        with open(snap_file, encoding="utf-8") as fh:
            snap_content = fh.read()

        up_content = git_show(upstream_ref, upstream_path)
        if up_content is None:
            for bid in list(entry["blocks"]):
                update_block_status(entry, bid, "removed")
            files_with_changes += 1
            continue

        snap_blocks = parse_blocks(snap_content)
        generate_block_ids(snap_blocks)
        up_blocks = parse_blocks(up_content)
        generate_block_ids(up_blocks)

        up_by_id = {b.block_id: b for b in up_blocks}
        changed = False

        for bid in list(entry["blocks"]):
            if bid in up_by_id:
                h = compute_block_hash(up_by_id[bid].lines)
                if h != entry["blocks"][bid]["en_hash"]:
                    update_block_status(entry, bid, "outdated")
                    changed = True
            else:
                update_block_status(entry, bid, "removed")
                changed = True

        for b in up_blocks:
            if b.block_id not in entry["blocks"]:
                entry["blocks"][b.block_id] = {
                    "type": b.block_type,
                    "en_hash": compute_block_hash(b.lines),
                    "status": "new",
                    "synced_at": None,
                }
                changed = True

        if changed:
            files_with_changes += 1

    return files_with_changes


# ---------------------------------------------------------------------------
# Inline sync-init (single file)
# ---------------------------------------------------------------------------


def _init_file_inline(
    manifest: dict,
    rel_path: str,
    upstream_ref: str,
    upstream_commit: str,
    *,
    force: bool = False,
) -> int:
    """Register a single file in the manifest. Returns block count."""
    if get_file_entry(manifest, rel_path) is not None and not force:
        return 0
    en_content = git_show(upstream_ref, rel_path)
    if en_content is None:
        return 0

    blocks = parse_blocks(en_content)
    generate_block_ids(blocks)
    block_data = [
        (b.block_id, b.block_type, compute_block_hash(b.lines))
        for b in blocks
    ]

    orig = os.path.join(ORIGINALS_DIR, rel_path)
    os.makedirs(os.path.dirname(orig), exist_ok=True)
    with open(orig, "w", encoding="utf-8") as fh:
        fh.write(en_content)
    add_file_entry(manifest, rel_path, rel_path, upstream_commit, block_data)
    return len(block_data)


# ---------------------------------------------------------------------------
# Inline sync-mark
# ---------------------------------------------------------------------------


def _sync_mark_inline(manifest: dict, upstream_ref: str) -> None:
    """Mark every outdated/new block as synced, update snapshots."""
    upstream_commit = git_rev_parse(upstream_ref)

    for rel_path in sorted(manifest["files"]):
        entry = manifest["files"][rel_path]
        upstream_path = entry.get("upstream_path", rel_path)

        en_content = git_show(upstream_ref, upstream_path)
        if en_content is None:
            continue

        blocks = parse_blocks(en_content)
        generate_block_ids(blocks)
        new_hashes = {
            b.block_id: compute_block_hash(b.lines) for b in blocks
        }

        to_remove: list[str] = []
        for bid, info in list(entry["blocks"].items()):
            if info["status"] == "removed":
                to_remove.append(bid)
            elif info["status"] in ("outdated", "new"):
                h = new_hashes.get(bid)
                if h:
                    update_block_status(entry, bid, "synced", en_hash=h)

        for bid in to_remove:
            remove_block_entry(entry, bid)

        snap = os.path.join(ORIGINALS_DIR, rel_path)
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w", encoding="utf-8") as fh:
            fh.write(en_content)
        entry["upstream_commit"] = upstream_commit


# ---------------------------------------------------------------------------
# Section rename detection (spec 9.6)
# ---------------------------------------------------------------------------


def _detect_section_renames(
    file_entry: dict,
    snapshot_blocks: list[Block],
    upstream_blocks: list[Block],
) -> dict[str, str]:
    """Return ``{new_section_path: old_section_path}`` for renames."""
    info = file_entry["blocks"]

    removed_secs: dict[str, list[str]] = {}
    new_secs: dict[str, list[str]] = {}
    for bid, bi in info.items():
        parts = bid.rsplit("/", 2)
        if len(parts) < 3:
            continue
        sec = parts[0]
        if bi["status"] == "removed":
            removed_secs.setdefault(sec, []).append(bid)
        elif bi["status"] == "new":
            new_secs.setdefault(sec, []).append(bid)

    if not removed_secs or not new_secs:
        return {}

    snap_by_id = {b.block_id: b for b in snapshot_blocks}
    up_by_id = {b.block_id: b for b in upstream_blocks}

    def _fps(bids: list[str], lookup: dict[str, Block]) -> frozenset | None:
        fps = []
        for bid in bids:
            b = lookup.get(bid)
            if not b:
                continue
            fp = extract_fingerprint(b)
            if fp and fp[0] in ("code", "attr", "table"):
                fps.append(fp)
        return frozenset(fps) if fps else None

    renames: dict[str, str] = {}
    used_old: set[str] = set()
    for new_sec, new_bids in new_secs.items():
        nfp = _fps(new_bids, up_by_id)
        if not nfp:
            continue
        for old_sec, old_bids in removed_secs.items():
            if old_sec in used_old:
                continue
            if _fps(old_bids, snap_by_id) == nfp:
                renames[new_sec] = old_sec
                used_old.add(old_sec)
                break
    return renames


# ---------------------------------------------------------------------------
# Process existing file with changes  (step 7)
# ---------------------------------------------------------------------------


def _process_file(
    rel_path: str,
    file_entry: dict,
    upstream_ref: str,
    provider_info: tuple,
    stats: dict,
    skipped: list,
    dry_run: bool,
) -> None:
    upstream_path = file_entry.get("upstream_path", rel_path)

    # 7a — read three versions
    snap_file = os.path.join(ORIGINALS_DIR, rel_path)
    if not os.path.exists(snap_file):
        return
    with open(snap_file, encoding="utf-8") as fh:
        snap_content = fh.read()
    up_content = git_show(upstream_ref, upstream_path)
    if up_content is None:
        return
    if not os.path.exists(rel_path):
        return
    with open(rel_path, encoding="utf-8") as fh:
        ja_content = fh.read()

    snap_blocks = parse_blocks(snap_content)
    generate_block_ids(snap_blocks)
    up_blocks = parse_blocks(up_content)
    generate_block_ids(up_blocks)
    ja_blocks = parse_blocks(ja_content)
    generate_block_ids(ja_blocks)

    # 7b — anchor matching  (snapshot EN <-> JA)
    matches = match_blocks(snap_blocks, ja_blocks)
    match_map: dict[int, int] = dict(matches)
    snap_id_to_idx = {b.block_id: i for i, b in enumerate(snap_blocks)}

    # 7c — section rename detection
    renames = _detect_section_renames(file_entry, snap_blocks, up_blocks)
    renames_consumed = set(renames.values())

    print(f"{rel_path}:")

    # 7d-7g — iterate upstream blocks and build new file
    new_contents: list[tuple[str, list[str]]] = []

    for up_idx, up_block in enumerate(up_blocks):
        up_id = up_block.block_id
        section_path = up_block.section_path
        bi = file_entry["blocks"].get(up_id, {})
        status = bi.get("status", "new")
        snap_idx = snap_id_to_idx.get(up_id)

        # ---- immutable / code blocks → always use upstream ----
        if up_block.block_type in _IMMUTABLE_TYPES:
            new_contents.append((up_block.block_type, list(up_block.lines)))
            if snap_idx is not None and status == "outdated":
                print(f"  COPIED      {up_id}  (code block updated)")
                stats["code_copied"] += 1
            elif snap_idx is not None:
                ja_idx = match_map.get(snap_idx)
                if (
                    ja_idx is not None
                    and 0 <= ja_idx < len(ja_blocks)
                    and compute_block_hash(ja_blocks[ja_idx].lines)
                    != compute_block_hash(snap_blocks[snap_idx].lines)
                ):
                    print(
                        f"  RESTORED    {up_id}  "
                        "(code block comment was translated, "
                        "restored to English)"
                    )
                    stats["code_restored"] += 1
            continue

        # ---- block exists in snapshot (synced / outdated) ----
        if snap_idx is not None:
            ja_idx = match_map.get(snap_idx)

            if status == "outdated":
                snap_block = snap_blocks[snap_idx]
                old_en = "\n".join(snap_block.lines)
                new_en = "\n".join(up_block.lines)
                cur_ja = (
                    "\n".join(ja_blocks[ja_idx].lines)
                    if ja_idx is not None and 0 <= ja_idx < len(ja_blocks)
                    else ""
                )
                sec_title = _get_section_title(up_blocks, up_block)
                prompt = _build_outdated_prompt(
                    sec_title, old_en, new_en, cur_ja
                )
                translated = _call_ai(provider_info, prompt)
                if translated:
                    new_contents.append(
                        (up_block.block_type, translated.strip().split("\n"))
                    )
                    print(
                        f"  TRANSLATED  {up_id}  (outdated → synced)"
                    )
                    stats["blocks_translated"] += 1
                else:
                    # fallback — keep current JA or upstream
                    if ja_idx is not None and 0 <= ja_idx < len(ja_blocks):
                        new_contents.append(
                            (up_block.block_type, list(ja_blocks[ja_idx].lines))
                        )
                    else:
                        new_contents.append(
                            (up_block.block_type, list(up_block.lines))
                        )
                    skipped.append((rel_path, up_id))
            else:
                # synced — keep JA
                if ja_idx is not None and 0 <= ja_idx < len(ja_blocks):
                    new_contents.append(
                        (up_block.block_type, list(ja_blocks[ja_idx].lines))
                    )
                else:
                    # structure mismatch: block missing from JA → translate
                    prompt = _build_new_file_prompt(
                        "\n".join(up_block.lines)
                    )
                    translated = _call_ai(provider_info, prompt)
                    if translated:
                        new_contents.append(
                            (
                                up_block.block_type,
                                translated.strip().split("\n"),
                            )
                        )
                        print(
                            f"  TRANSLATED  {up_id}  "
                            "(structure fix, missing block)"
                        )
                        stats["blocks_translated"] += 1
                    else:
                        new_contents.append(
                            (up_block.block_type, list(up_block.lines))
                        )
                        skipped.append((rel_path, up_id))
            continue

        # ---- new block (not in snapshot) ----
        # check section rename first
        if section_path in renames:
            old_sec = renames[section_path]
            if up_block.block_type == "section_header":
                prompt = _build_new_file_prompt("\n".join(up_block.lines))
                translated = _call_ai(provider_info, prompt)
                if translated:
                    new_contents.append(
                        (up_block.block_type, translated.strip().split("\n"))
                    )
                    print(f"  TRANSLATED  {up_id}  (section renamed)")
                    stats["blocks_translated"] += 1
                else:
                    new_contents.append(
                        (up_block.block_type, list(up_block.lines))
                    )
                    skipped.append((rel_path, up_id))
            else:
                # reuse JA from old section
                suffix = up_id[len(section_path):]  # /type/ordinal
                old_id = old_sec + suffix
                old_snap_idx = snap_id_to_idx.get(old_id)
                reused = False
                if old_snap_idx is not None:
                    old_ja_idx = match_map.get(old_snap_idx)
                    if (
                        old_ja_idx is not None
                        and 0 <= old_ja_idx < len(ja_blocks)
                    ):
                        new_contents.append(
                            (
                                up_block.block_type,
                                list(ja_blocks[old_ja_idx].lines),
                            )
                        )
                        reused = True
                if not reused:
                    sec_title = _get_section_title(up_blocks, up_block)
                    prev_ja = _find_adjacent_ja(
                        up_blocks, up_idx, -1,
                        snap_id_to_idx, match_map, ja_blocks,
                    )
                    next_ja = _find_adjacent_ja(
                        up_blocks, up_idx, 1,
                        snap_id_to_idx, match_map, ja_blocks,
                    )
                    prompt = _build_new_prompt(
                        sec_title, "\n".join(up_block.lines),
                        prev_ja, next_ja,
                    )
                    translated = _call_ai(provider_info, prompt)
                    if translated:
                        new_contents.append(
                            (
                                up_block.block_type,
                                translated.strip().split("\n"),
                            )
                        )
                        print(
                            f"  TRANSLATED  {up_id}  (new → synced)"
                        )
                        stats["blocks_translated"] += 1
                    else:
                        new_contents.append(
                            (up_block.block_type, list(up_block.lines))
                        )
                        skipped.append((rel_path, up_id))
            continue

        # regular new block
        sec_title = _get_section_title(up_blocks, up_block)
        prev_ja = _find_adjacent_ja(
            up_blocks, up_idx, -1,
            snap_id_to_idx, match_map, ja_blocks,
        )
        next_ja = _find_adjacent_ja(
            up_blocks, up_idx, 1,
            snap_id_to_idx, match_map, ja_blocks,
        )
        prompt = _build_new_prompt(
            sec_title, "\n".join(up_block.lines), prev_ja, next_ja,
        )
        translated = _call_ai(provider_info, prompt)
        if translated:
            new_contents.append(
                (up_block.block_type, translated.strip().split("\n"))
            )
            print(f"  TRANSLATED  {up_id}  (new → synced)")
            stats["blocks_translated"] += 1
        else:
            new_contents.append(
                (up_block.block_type, list(up_block.lines))
            )
            skipped.append((rel_path, up_id))

    # report removed blocks
    for bid, bi in file_entry["blocks"].items():
        if bi["status"] == "removed":
            sec = bid.rsplit("/", 2)[0]
            if sec not in renames_consumed:
                print(
                    f"  REMOVED     {bid}  (removed from upstream)"
                )
                stats["blocks_removed"] += 1

    # 7k — write
    if not dry_run and new_contents:
        with open(rel_path, "w", encoding="utf-8") as fh:
            fh.write(_reconstruct_file(new_contents))

    stats["files_updated"] += 1


# ---------------------------------------------------------------------------
# Translate entirely new file  (step 4 per-file)
# ---------------------------------------------------------------------------


def _translate_new_file(
    rel_path: str,
    upstream_ref: str,
    upstream_commit: str,
    manifest: dict,
    provider_info: tuple,
    stats: dict,
    skipped: list,
    dry_run: bool,
) -> None:
    en_content = git_show(upstream_ref, rel_path)
    if en_content is None:
        return

    # init in manifest + snapshot
    if not dry_run:
        _init_file_inline(manifest, rel_path, upstream_ref, upstream_commit)

    blocks = parse_blocks(en_content)
    generate_block_ids(blocks)

    new_contents: list[tuple[str, list[str]]] = []
    translated_count = 0

    for block in blocks:
        if block.block_type in _IMMUTABLE_TYPES:
            new_contents.append((block.block_type, list(block.lines)))
        else:
            prompt = _build_new_file_prompt("\n".join(block.lines))
            translated = _call_ai(provider_info, prompt)
            if translated:
                new_contents.append(
                    (block.block_type, translated.strip().split("\n"))
                )
                translated_count += 1
            else:
                new_contents.append((block.block_type, list(block.lines)))
                skipped.append((rel_path, block.block_id))

    if not dry_run and new_contents:
        os.makedirs(os.path.dirname(rel_path), exist_ok=True)
        with open(rel_path, "w", encoding="utf-8") as fh:
            fh.write(_reconstruct_file(new_contents))

    print(f"NEW FILE    {rel_path} ({translated_count} blocks translated)")
    stats["new_files"] += 1


# ---------------------------------------------------------------------------
# Detection — new / deleted files and modules
# ---------------------------------------------------------------------------


def _upstream_page_files(upstream_ref: str) -> list[str]:
    """List all .adoc page files (and nav.adoc) in upstream."""
    all_files = git_ls_tree(upstream_ref, "modules/")
    return [
        f
        for f in all_files
        if f.endswith(".adoc") and ("/pages/" in f or f.endswith("/nav.adoc"))
    ]


def _upstream_modules(upstream_ref: str) -> set[str]:
    files = git_ls_tree(upstream_ref, "modules/")
    mods: set[str] = set()
    for f in files:
        parts = f.split("/")
        if len(parts) >= 2:
            mods.add(f"modules/{parts[1]}")
    return mods


def _ja_modules() -> set[str]:
    mods: set[str] = set()
    if os.path.isdir("modules"):
        for d in os.listdir("modules"):
            if os.path.isdir(os.path.join("modules", d)):
                mods.add(f"modules/{d}")
    return mods


def _detect_new(
    manifest: dict, upstream_ref: str
) -> tuple[list[str], list[str]]:
    """Return ``(new_page_files, new_module_paths)``."""
    up_pages = [
        f for f in _upstream_page_files(upstream_ref) if "/pages/" in f
    ]
    up_mods = _upstream_modules(upstream_ref)
    ja_mods = _ja_modules()
    new_modules = sorted(up_mods - ja_mods)

    managed = set(manifest["files"])
    new_files: list[str] = []
    for f in sorted(up_pages):
        mod = "/".join(f.split("/")[:2])
        if mod not in new_modules and f not in managed and not os.path.exists(f):
            new_files.append(f)
    return new_files, new_modules


def _detect_deleted(
    manifest: dict, upstream_ref: str
) -> tuple[list[str], list[str]]:
    """Return ``(deleted_files, deleted_module_paths)``."""
    up_files_set = set(git_ls_tree(upstream_ref, "modules/"))
    up_mods = _upstream_modules(upstream_ref)
    ja_mods = _ja_modules()

    deleted_modules = sorted(ja_mods - up_mods)

    # JA page files not in upstream (excluding modules being deleted entirely)
    deleted_files: list[str] = []
    for root, _dirs, files in os.walk("modules"):
        for fname in files:
            if not fname.endswith(".adoc"):
                continue
            path = os.path.join(root, fname)
            if "/pages/" not in path:
                continue
            mod = "/".join(path.split("/")[:2])
            if mod in deleted_modules:
                continue
            if path not in up_files_set:
                deleted_files.append(path)
    return sorted(deleted_files), deleted_modules


# ---------------------------------------------------------------------------
# Delete helpers
# ---------------------------------------------------------------------------


def _delete_file(
    rel_path: str, manifest: dict, stats: dict, dry_run: bool
) -> None:
    if not dry_run:
        if os.path.exists(rel_path):
            os.remove(rel_path)
        snap = os.path.join(ORIGINALS_DIR, rel_path)
        if os.path.exists(snap):
            os.remove(snap)
        remove_file_entry(manifest, rel_path)
    print(f"DELETED     {rel_path} (no upstream counterpart)")
    stats["deleted_files"] += 1


def _delete_module(
    module_path: str, manifest: dict, stats: dict, dry_run: bool
) -> None:
    if not dry_run:
        if os.path.isdir(module_path):
            shutil.rmtree(module_path)
        snap_dir = os.path.join(ORIGINALS_DIR, module_path)
        if os.path.isdir(snap_dir):
            shutil.rmtree(snap_dir)
        for key in list(manifest["files"]):
            if key.startswith(module_path + "/"):
                remove_file_entry(manifest, key)
    mod_name = module_path.split("/")[-1]
    print(f"DELETED     {module_path}/ (entire module, no upstream counterpart)")
    stats["deleted_modules"] += 1


# ---------------------------------------------------------------------------
# Create new module  (step 4 per-module)
# ---------------------------------------------------------------------------


def _create_new_module(
    module_path: str,
    upstream_ref: str,
    upstream_commit: str,
    manifest: dict,
    provider_info: tuple,
    stats: dict,
    skipped: list,
    dry_run: bool,
) -> None:
    if not dry_run:
        for sub in ("pages", "attachments", "images"):
            os.makedirs(os.path.join(module_path, sub), exist_ok=True)

    # nav.adoc
    nav_path = os.path.join(module_path, "nav.adoc")
    up_nav = git_show(upstream_ref, nav_path)
    if up_nav:
        _translate_and_write_nav(
            nav_path, up_nav, provider_info, skipped, dry_run
        )
        print(f"NAV SYNC    {nav_path}: created (new module)")
        stats["nav_synced"] += 1

    # page files
    pages = [
        f
        for f in git_ls_tree(upstream_ref, module_path + "/pages/")
        if f.endswith(".adoc")
    ]
    for page in sorted(pages):
        _translate_new_file(
            page, upstream_ref, upstream_commit, manifest,
            provider_info, stats, skipped, dry_run,
        )

    # attachments + images
    _copy_upstream_assets(module_path, upstream_ref, stats, dry_run)


def _copy_upstream_assets(
    module_path: str,
    upstream_ref: str,
    stats: dict,
    dry_run: bool,
) -> None:
    for sub in ("attachments", "images"):
        prefix = os.path.join(module_path, sub)
        for fpath in git_ls_tree(upstream_ref, prefix + "/"):
            content = git_show_binary(upstream_ref, fpath)
            if content is None:
                continue
            if not dry_run:
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "wb") as fh:
                    fh.write(content)
            print(f"COPIED      {fpath}")
            stats["assets_copied"] += 1


# ---------------------------------------------------------------------------
# Nav.adoc sync  (step 8)
# ---------------------------------------------------------------------------


def _translate_and_write_nav(
    nav_path: str,
    upstream_content: str,
    provider_info: tuple,
    skipped: list,
    dry_run: bool,
) -> None:
    """Translate display text of xref lines and write nav.adoc."""
    new_lines: list[str] = []
    for line in upstream_content.rstrip("\n").split("\n"):
        display = _extract_xref_display(line)
        target = _extract_xref_target(line)
        if target and display:
            translated = _call_ai(provider_info, _build_new_file_prompt(display))
            if translated:
                new_lines.append(
                    line.replace(f"[{display}]", f"[{translated.strip()}]")
                )
            else:
                new_lines.append(line)
                skipped.append((nav_path, f"nav: {display}"))
        else:
            new_lines.append(line)
    if not dry_run:
        os.makedirs(os.path.dirname(nav_path), exist_ok=True)
        with open(nav_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + "\n")


def _sync_nav(
    module_path: str,
    upstream_ref: str,
    provider_info: tuple,
    stats: dict,
    skipped: list,
    dry_run: bool,
) -> None:
    """Sync a module's nav.adoc with upstream (step 8)."""
    nav_path = os.path.join(module_path, "nav.adoc")
    up_nav = git_show(upstream_ref, nav_path)
    if up_nav is None:
        return

    # build JA xref-target -> line mapping
    ja_by_target: dict[str, str] = {}
    if os.path.exists(nav_path):
        with open(nav_path, encoding="utf-8") as fh:
            for line in fh.read().rstrip("\n").split("\n"):
                t = _extract_xref_target(line)
                if t:
                    ja_by_target[t] = line

    up_lines = up_nav.rstrip("\n").split("\n")
    up_targets: set[str] = set()
    new_lines: list[str] = []
    added = 0

    for up_line in up_lines:
        t = _extract_xref_target(up_line)
        if t:
            up_targets.add(t)
        if t and t in ja_by_target:
            new_lines.append(ja_by_target[t])
        elif t:
            # new xref — translate display text
            display = _extract_xref_display(up_line)
            if display:
                translated = _call_ai(
                    provider_info, _build_new_file_prompt(display)
                )
                if translated:
                    new_lines.append(
                        up_line.replace(
                            f"[{display}]", f"[{translated.strip()}]"
                        )
                    )
                else:
                    new_lines.append(up_line)
                    skipped.append((nav_path, f"nav: {display}"))
            else:
                new_lines.append(up_line)
            added += 1
        else:
            new_lines.append(up_line)

    removed = sum(1 for t in ja_by_target if t not in up_targets)

    if added or removed:
        if not dry_run:
            with open(nav_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(new_lines) + "\n")
        parts = []
        if added:
            parts.append(f"+{added} xref lines added")
        if removed:
            parts.append(f"-{removed} removed")
        print(f"NAV SYNC    {nav_path}: {', '.join(parts)}")
        stats["nav_synced"] += 1


# ---------------------------------------------------------------------------
# antora.yml sync  (step 9)
# ---------------------------------------------------------------------------


def _sync_antora_yml(
    upstream_ref: str,
    stats: dict,
    dry_run: bool,
) -> None:
    up_content = git_show(upstream_ref, "antora.yml")
    if not up_content:
        return
    if not os.path.exists("antora.yml"):
        return

    with open("antora.yml", encoding="utf-8") as fh:
        ja_content = fh.read()

    # extract nav lists
    def _nav_entries(text: str) -> list[str]:
        entries: list[str] = []
        in_nav = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "nav:" or stripped.startswith("nav:"):
                in_nav = True
                continue
            if in_nav:
                if stripped.startswith("- "):
                    entries.append(stripped[2:].strip())
                elif stripped:
                    break
        return entries

    up_nav = _nav_entries(up_content)
    ja_nav = _nav_entries(ja_content)

    if up_nav == ja_nav:
        return

    # rebuild antora.yml keeping JA name, using upstream nav
    new_lines: list[str] = []
    in_nav = False
    nav_written = False
    for line in ja_content.split("\n"):
        stripped = line.strip()
        if stripped == "nav:" or stripped.startswith("nav:"):
            in_nav = True
            new_lines.append("nav:")
            for entry in up_nav:
                new_lines.append(f"- {entry}")
            nav_written = True
            continue
        if in_nav:
            if stripped.startswith("- "):
                continue
            in_nav = False
        # update title/version from upstream
        if stripped.startswith("title:"):
            for ul in up_content.split("\n"):
                if ul.strip().startswith("title:"):
                    new_lines.append(ul)
                    break
            else:
                new_lines.append(line)
            continue
        if stripped.startswith("version:"):
            for ul in up_content.split("\n"):
                if ul.strip().startswith("version:"):
                    new_lines.append(ul)
                    break
            else:
                new_lines.append(line)
            continue
        new_lines.append(line)

    new_text = "\n".join(new_lines)
    # ensure trailing newline, no double
    if not new_text.endswith("\n"):
        new_text += "\n"

    if new_text != ja_content:
        if not dry_run:
            with open("antora.yml", "w", encoding="utf-8") as fh:
                fh.write(new_text)
        added = set(up_nav) - set(ja_nav)
        removed = set(ja_nav) - set(up_nav)
        for a in sorted(added):
            mod = a.split("/")[1] if "/" in a else a
            print(f"ANTORA      antora.yml: +1 module added ({mod})")
        for r in sorted(removed):
            mod = r.split("/")[1] if "/" in r else r
            print(f"ANTORA      antora.yml: -1 module removed ({mod})")
        stats["antora_updated"] = True


# ---------------------------------------------------------------------------
# Module asset sync  (step 7i/7j)
# ---------------------------------------------------------------------------


def _sync_module_assets(
    module_path: str,
    upstream_ref: str,
    stats: dict,
    dry_run: bool,
) -> None:
    """Compare attachments/ and images/ with upstream and sync."""
    for sub in ("attachments", "images"):
        prefix = os.path.join(module_path, sub)

        up_files = set(git_ls_tree(upstream_ref, prefix + "/"))

        local_files: set[str] = set()
        if os.path.isdir(prefix):
            for root, _dirs, files in os.walk(prefix):
                for fname in files:
                    local_files.add(os.path.join(root, fname))

        label = "IMAGE SYNC" if sub == "images" else "ASSET SYNC"

        # copy / update from upstream
        for fpath in sorted(up_files):
            up_bytes = git_show_binary(upstream_ref, fpath)
            if up_bytes is None:
                continue
            if os.path.exists(fpath):
                with open(fpath, "rb") as fh:
                    if fh.read() == up_bytes:
                        continue
                action = "updated from upstream"
            else:
                action = "copied from upstream"
            if not dry_run:
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "wb") as fh:
                    fh.write(up_bytes)
            print(f"{label}  {fpath}: {action}")
            stats["assets_updated"] += 1

        # delete local files absent from upstream
        for fpath in sorted(local_files):
            if fpath not in up_files:
                if not dry_run:
                    os.remove(fpath)
                print(f"{label}  {fpath}: deleted (not in upstream)")
                stats["assets_deleted"] += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="翻訳結果を stdout に表示するが、ファイルへの書き込み・"
        "ブランチ作成・sync-mark の実行を行わない",
    )
    parser.add_argument(
        "--format",
        action="store_true",
        help="翻訳適用後に add-jp-lat-spaces.py と "
        "convert-fullwidth-parens.py を自動実行",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "claude", "litellm"],
        default="gemini",
        help="翻訳 API プロバイダー (デフォルト: gemini)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="AI モデル (デフォルト: provider に依存)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="作成するブランチ名 (デフォルト: translate/YYYY-MM-DD)",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="LiteLLM 用カスタム API ベース URL (OpenAI 互換エンドポイント)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        help="処理対象のファイルまたはディレクトリ (省略時: 全管理ファイル)",
    )
    args = parser.parse_args()

    # ---- validate provider ----
    provider_info = _validate_provider(args.provider, args.model, args.api_base)

    # ---- check manifest ----
    if not os.path.exists(MANIFEST_PATH):
        print(
            "Error: manifest.json not found. Run sync-init.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = load_manifest()

    # ---- step 1: working directory clean ----
    if not args.dry_run:
        if not git_status_clean():
            print(
                "Error: Working directory has uncommitted changes. "
                "Commit or stash them first.",
                file=sys.stderr,
            )
            sys.exit(1)

    # ---- step 2: create branch ----
    branch = args.branch or f"translate/{datetime.date.today().isoformat()}"
    if not args.dry_run:
        if git_branch_exists(branch):
            print(
                f"Error: Branch '{branch}' already exists. "
                "Use --branch to specify a different name.",
                file=sys.stderr,
            )
            sys.exit(1)
        start = git_current_branch()
        git_switch_create_branch(branch, start)
        print(f"BRANCH      Created branch '{branch}' from {start}")

    # ---- step 3: inline sync-check ----
    files_changed = _sync_check_inline(manifest, UPSTREAM_REF)
    print(
        f"CHECKED     sync-check.py executed "
        f"(fetched upstream, {files_changed} files with changes)"
    )
    if not args.dry_run:
        save_manifest(manifest)

    # ---- path filter helper ----
    def _in_scope(fpath: str) -> bool:
        if not args.paths:
            return True
        for p in args.paths:
            p_norm = p.rstrip("/")
            if fpath == p_norm or fpath.startswith(p_norm + "/"):
                return True
        return False

    # ---- prepare stats ----
    stats: dict = {
        "new_files": 0,
        "new_modules": 0,
        "deleted_files": 0,
        "deleted_modules": 0,
        "files_updated": 0,
        "blocks_translated": 0,
        "code_copied": 0,
        "code_restored": 0,
        "blocks_removed": 0,
        "assets_copied": 0,
        "assets_updated": 0,
        "assets_deleted": 0,
        "nav_synced": 0,
        "antora_updated": False,
    }
    skipped: list[tuple[str, str]] = []

    upstream_commit = git_rev_parse(UPSTREAM_REF)

    # ---- step 4: new files / modules ----
    new_files, new_modules = _detect_new(manifest, UPSTREAM_REF)
    if args.paths:
        new_files = [f for f in new_files if _in_scope(f)]
        new_modules = [m for m in new_modules if _in_scope(m)]

    for mod in new_modules:
        _create_new_module(
            mod, UPSTREAM_REF, upstream_commit, manifest,
            provider_info, stats, skipped, args.dry_run,
        )

    for fpath in new_files:
        _translate_new_file(
            fpath, UPSTREAM_REF, upstream_commit, manifest,
            provider_info, stats, skipped, args.dry_run,
        )

    # ---- step 5: deleted files / modules ----
    del_files, del_modules = _detect_deleted(manifest, UPSTREAM_REF)
    if args.paths:
        del_files = [f for f in del_files if _in_scope(f)]
        del_modules = [m for m in del_modules if _in_scope(m)]

    for fpath in del_files:
        _delete_file(fpath, manifest, stats, args.dry_run)

    for mod in del_modules:
        _delete_module(mod, manifest, stats, args.dry_run)

    if not args.dry_run:
        save_manifest(manifest)

    # ---- step 6: find files with changes ----
    files_to_process = [
        (path, manifest["files"][path])
        for path in sorted(manifest["files"])
        if _in_scope(path) and any(
            b["status"] in ("outdated", "new", "removed")
            for b in manifest["files"][path]["blocks"].values()
        )
    ]

    has_work = bool(
        files_to_process
        or new_files
        or new_modules
        or del_files
        or del_modules
    )
    if not has_work:
        print("\nNo outdated or new blocks found. Nothing to translate.")
        return

    # ---- step 7: process each file with changes ----
    modules_processed: set[str] = set()
    for rel_path, file_entry in files_to_process:
        _process_file(
            rel_path, file_entry, UPSTREAM_REF,
            provider_info, stats, skipped, args.dry_run,
        )
        parts = rel_path.split("/")
        if len(parts) >= 2:
            modules_processed.add("/".join(parts[:2]))

    # ---- step 7i/7j: asset sync for processed modules ----
    for mod in sorted(modules_processed):
        _sync_module_assets(mod, UPSTREAM_REF, stats, args.dry_run)

    # ---- step 8: nav.adoc sync ----
    all_mods = _upstream_modules(UPSTREAM_REF) & _ja_modules()
    for mod in sorted(all_mods):
        _sync_nav(
            mod, UPSTREAM_REF, provider_info, stats, skipped, args.dry_run,
        )

    # ---- step 9: antora.yml sync ----
    _sync_antora_yml(UPSTREAM_REF, stats, args.dry_run)

    # ---- step 10: format ----
    if args.format and not args.dry_run:
        for script in ("add-jp-lat-spaces.py", "convert-fullwidth-parens.py"):
            script_path = os.path.join("tools", "translation", script)
            if os.path.exists(script_path):
                subprocess.run(
                    [sys.executable, script_path], check=False,
                )
                print(f"FORMATTED   {script} applied")

    # ---- step 11: sync-mark ----
    if skipped:
        print(f"\nSkipped blocks ({len(skipped)}):")
        for path, bid in skipped:
            print(f"  {path}: {bid}")
        print("sync-mark.py NOT executed due to skipped blocks")
    elif not args.dry_run:
        _sync_mark_inline(manifest, UPSTREAM_REF)
        save_manifest(manifest)
        print("MARKED      sync-mark.py executed")

    # ---- step 12: summary ----
    parts_list: list[str] = []
    if stats["new_files"]:
        parts_list.append(f"{stats['new_files']} new files translated")
    if stats["files_updated"]:
        parts_list.append(f"{stats['files_updated']} files updated")
    if stats["blocks_translated"]:
        parts_list.append(
            f"{stats['blocks_translated']} blocks translated"
        )
    if stats["code_copied"]:
        parts_list.append(f"{stats['code_copied']} code blocks copied")
    if stats["blocks_removed"]:
        parts_list.append(f"{stats['blocks_removed']} blocks removed")
    if stats["code_restored"]:
        parts_list.append(
            f"{stats['code_restored']} code blocks restored"
        )
    if stats["assets_copied"]:
        parts_list.append(f"{stats['assets_copied']} assets copied")
    if stats["assets_updated"]:
        parts_list.append(
            f"{stats['assets_updated']} attachments updated"
        )
    if stats["assets_deleted"]:
        parts_list.append(f"{stats['assets_deleted']} assets deleted")
    if stats["deleted_files"]:
        parts_list.append(f"{stats['deleted_files']} files deleted")
    if stats["deleted_modules"]:
        parts_list.append(f"{stats['deleted_modules']} modules deleted")

    if parts_list:
        print()
        print(", ".join(parts_list))

    if not args.dry_run:
        print()
        print("Review your changes:")
        print("  git diff")
        print("  git diff --stat")
        print("When satisfied, commit and push manually.")


if __name__ == "__main__":
    main()
