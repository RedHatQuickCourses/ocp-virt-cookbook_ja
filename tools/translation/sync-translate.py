#!/usr/bin/env python3
"""AI による自動翻訳スクリプト。

sync-check.py で検知された outdated / new ブロックを Gemini API で自動翻訳し、
日本語ファイルに直接適用する。翻訳作業は現在のブランチから作成した新規ローカル
ブランチ上で行い、人間がレビューできる状態で停止する (git add / git commit /
git push は行わない)。

使い方:
  # 全管理対象ファイルの outdated/new ブロックを翻訳 (デフォルトモデル: gemini-3.7-flash)
  python3 tools/translation/sync-translate.py

  # モデルを指定
  python3 tools/translation/sync-translate.py --model gemini-2.5-pro

  # dry-run (翻訳結果を表示するが適用しない、ブランチも作成しない)
  python3 tools/translation/sync-translate.py --dry-run

  # 既存の書式整形ツールも自動実行
  python3 tools/translation/sync-translate.py --format

  # ブランチ名を指定
  python3 tools/translation/sync-translate.py --branch translate/2026-08-12

  # 中断した翻訳を再開
  python3 tools/translation/sync-translate.py --resume
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import glob
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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
PROGRESS_FILE = "tools/translation/.translate-progress.json"
_IMMUTABLE_TYPES = frozenset({"literal_block", "block_attribute"})
_RE_COMMENT_LINE = re.compile(r"^(\s*#\s?)(.*)")
_NO_TRAILING_BLANK = frozenset({"block_attribute", "block_title"})
_RE_ADMONITION_PREFIX = re.compile(
    r"^(NOTE|WARNING|IMPORTANT|CAUTION|TIP):\s*"
)
_TRANSLATED_ADMONITION_MAP: dict[str, str] = {
    "注": "NOTE", "注記": "NOTE", "注意": "CAUTION",
    "重要": "IMPORTANT", "ヒント": "TIP", "警告": "WARNING",
}
_HEADING_GLOSSARY: dict[str, str] = {
    "See Also": "参照",
    "Additional Resources": "追加リソース",
    "Summary": "まとめ",
    "Prerequisites": "前提条件",
    "Cleanup": "クリーンアップ",
    "Overview": "概要",
    "Troubleshooting": "トラブルシューティング",
    "Next Steps": "次のステップ",
    "Best Practices": "ベストプラクティス",
}

# ---------------------------------------------------------------------------
# Progress file for --resume
# ---------------------------------------------------------------------------

_interrupted = False


def _sigint_handler(signum, frame):
    global _interrupted
    _interrupted = True
    print("\n\nInterrupted. To resume later:\n"
          "  python3 tools/translation/sync-translate.py --resume\n",
          file=sys.stderr, flush=True)
    sys.exit(130)


def _load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as fh:
        json.dump(progress, fh, ensure_ascii=False, indent=2)


def _init_progress(branch: str, model: str) -> dict:
    progress = {
        "branch": branch,
        "model": model,
        "started_at": datetime.datetime.now().isoformat(),
        "sync_check_done": False,
        "completed_files": [],
        "completed_nav": [],
        "completed_modules": [],
        "completed_assets": [],
    }
    _save_progress(progress)
    return progress


def _delete_progress() -> None:
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Gemini REST API — setup, rate control, and call
# ---------------------------------------------------------------------------

_AI_TIMEOUT = 120  # seconds
_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
)


def _validate_gemini(model: str | None) -> tuple[str, str]:
    """Validate Gemini setup and return ``(api_key, model)``."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "Error: GEMINI_API_KEY environment variable not set",
            file=sys.stderr,
        )
        sys.exit(1)
    return (api_key, model or "gemini-3.7-flash")


class _RateState:
    """Thread-safe rate limit state from Gemini response headers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rpm_limit: int = 0
        self.tpm_limit: int = 0
        self.remaining_rpm: int = 0
        self.remaining_tpm: int = 0
        self.request_count: int = 0
        self.batch_size: int = 5  # default until first response

    def update_from_headers(self, headers: dict[str, str]) -> None:
        with self._lock:
            for key, val in headers.items():
                lk = key.lower()
                if lk == "x-ratelimit-limit-requests":
                    self.rpm_limit = int(val)
                elif lk == "x-ratelimit-limit-tokens":
                    self.tpm_limit = int(val)
                elif lk == "x-ratelimit-remaining-requests":
                    self.remaining_rpm = int(val)
                elif lk == "x-ratelimit-remaining-tokens":
                    self.remaining_tpm = int(val)
            self.request_count += 1
            if self.rpm_limit and self.tpm_limit and self.request_count == 1:
                self.batch_size = max(1, min(
                    self.rpm_limit // 2,
                    self.tpm_limit // 4000,
                    15,
                ))

    def adaptive_wait(self) -> float:
        with self._lock:
            if self.remaining_rpm <= 0 and self.rpm_limit > 0:
                return 60.0
            if self.remaining_tpm <= 0 and self.tpm_limit > 0:
                return 60.0
            if self.rpm_limit and self.remaining_rpm < self.rpm_limit * 0.2:
                return max(0.5, 60.0 / max(self.remaining_rpm, 1))
            return 0.0

    def halve_batch(self) -> None:
        with self._lock:
            self.batch_size = max(1, self.batch_size // 2)

    def status_line(self) -> str:
        with self._lock:
            if not self.rpm_limit:
                return ""
            return (
                f"RPM: {self.remaining_rpm}/{self.rpm_limit}, "
                f"TPM: {self.remaining_tpm}/{self.tpm_limit} "
                f"(batch_size={self.batch_size})"
            )


_rate_state = _RateState()


def _call_ai(
    gemini_info: tuple[str, str], prompt: str
) -> str | None:
    """Call Gemini REST API with timeout and retries.

    Returns translated text, or ``None`` on failure.
    """
    api_key, model = gemini_info
    url = f"{_GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
    }).encode("utf-8")

    consecutive_429 = 0

    for attempt in range(3):
        wait = _rate_state.adaptive_wait()
        if wait > 0:
            time.sleep(wait)

        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_AI_TIMEOUT) as resp:
                hdrs = {k: v for k, v in resp.getheaders()}
                _rate_state.update_from_headers(hdrs)
                data = json.loads(resp.read().decode("utf-8"))
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text")
                )
                if _rate_state.request_count % 10 == 0:
                    status = _rate_state.status_line()
                    if status:
                        print(f"RATE LIMIT  {status}", flush=True)
                return text
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                consecutive_429 += 1
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    wait_s = float(retry_after)
                else:
                    wait_s = 2 ** (attempt + 1)
                if consecutive_429 >= 3:
                    _rate_state.halve_batch()
                    print(
                        f"    429 x{consecutive_429}, batch halved to "
                        f"{_rate_state.batch_size}",
                        file=sys.stderr, flush=True,
                    )
                print(
                    f"    Rate limited (429), waiting {wait_s:.0f}s... "
                    f"(attempt {attempt + 1}/3)",
                    file=sys.stderr, flush=True,
                )
                time.sleep(wait_s)
            elif code in (500, 503):
                wait_s = 2 ** (attempt + 1)
                print(
                    f"    Server error ({code}), retrying in {wait_s}s...",
                    file=sys.stderr, flush=True,
                )
                time.sleep(wait_s)
            else:
                err_body = exc.read().decode("utf-8", errors="replace")[:200]
                print(
                    f"    API error ({code}): {err_body}",
                    file=sys.stderr, flush=True,
                )
                return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            wait_s = 2 ** (attempt + 1)
            print(
                f"    Network error: {exc}, retrying in {wait_s}s... "
                f"(attempt {attempt + 1}/3)",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait_s)

    print("    Failed after 3 retries", file=sys.stderr, flush=True)
    return None


# ---------------------------------------------------------------------------
# Batch translation
# ---------------------------------------------------------------------------

_BLOCK_DELIM_RE = re.compile(r"===BLOCK_(\d+)===")


def _build_batch_prompt(
    blocks_text: list[str], base_rules: str
) -> str:
    """Build a batched prompt with ===BLOCK_N=== delimiters."""
    parts = [base_rules]
    parts.append(
        "\n- 各ブロックは ===BLOCK_N=== で区切られています"
        "\n- 翻訳結果も同じ ===BLOCK_N=== 区切りで出力してください"
        "\n- ブロック数を変えないでください\n"
    )
    for i, text in enumerate(blocks_text, 1):
        parts.append(f"\n===BLOCK_{i}===\n{text}")
    return "\n".join(parts)


def _parse_batch_response(
    response: str, expected_count: int
) -> list[str] | None:
    """Parse a batched response into individual blocks.

    Returns a list of translated texts, or None if parsing fails.
    """
    chunks: list[tuple[int, str]] = []
    current_idx = -1
    current_lines: list[str] = []

    for line in response.split("\n"):
        m = _BLOCK_DELIM_RE.match(line.strip())
        if m:
            if current_idx >= 0:
                chunks.append((current_idx, "\n".join(current_lines).strip()))
            current_idx = int(m.group(1))
            current_lines = []
        else:
            current_lines.append(line)

    if current_idx >= 0:
        chunks.append((current_idx, "\n".join(current_lines).strip()))

    if len(chunks) != expected_count:
        return None

    chunks.sort(key=lambda x: x[0])
    for i, (idx, _) in enumerate(chunks, 1):
        if idx != i:
            return None

    results = [text for _, text in chunks]
    if any(not t for t in results):
        return None
    return results


def _call_ai_batch(
    gemini_info: tuple[str, str],
    blocks_text: list[str],
    base_rules: str,
) -> list[str | None]:
    """Translate multiple blocks in one API call with fallback.

    Returns a list of translated texts (same length as blocks_text).
    Individual entries may be None on failure.
    """
    if len(blocks_text) == 1:
        prompt = base_rules + "\n\n## 翻訳対象の英語:\n" + blocks_text[0]
        result = _call_ai(gemini_info, prompt)
        return [result]

    prompt = _build_batch_prompt(blocks_text, base_rules)
    response = _call_ai(gemini_info, prompt)

    if response:
        parsed = _parse_batch_response(response, len(blocks_text))
        if parsed:
            return parsed

    results: list[str | None] = []
    for text in blocks_text:
        prompt = base_rules + "\n\n## 翻訳対象の英語:\n" + text
        result = _call_ai(gemini_info, prompt)
        results.append(result)
    return results


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
        "- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, "
        "IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。"
        "日本語に翻訳しないこと\n"
        "- インラインアドモニション (NOTE: テキスト) とブロックアドモニション "
        "([NOTE]\\n====) の形式を相互に変換しないこと\n"
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
        "- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, "
        "IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。"
        "日本語に翻訳しないこと\n"
        "- インラインアドモニション (NOTE: テキスト) とブロックアドモニション "
        "([NOTE]\\n====) の形式を相互に変換しないこと\n"
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
        "- AsciiDoc のアドモニションキーワード (NOTE:, WARNING:, "
        "IMPORTANT:, TIP:, CAUTION:) は英語のまま維持すること。"
        "日本語に翻訳しないこと\n"
        "- インラインアドモニション (NOTE: テキスト) とブロックアドモニション "
        "([NOTE]\\n====) の形式を相互に変換しないこと\n"
        "- 翻訳文のみを出力し、説明や注記は付けないこと\n"
        "- 自然な日本語で、技術的に正確な翻訳を行うこと\n\n"
        f"## 翻訳対象の英語:\n{en_text}"
    )


def _build_code_comment_prompt(en_code: str, current_ja: str | None = None) -> str:
    base = (
        "あなたは技術文書の翻訳者です。以下のコードブロック内の"
        "コメント行 (# で始まる行) のみを日本語に翻訳してください。\n\n"
        "## ルール\n"
        "- コメント行 (# で始まる行) のみを翻訳すること\n"
        "- コメント行以外のすべての行 (コマンド、YAML、変数、空行等) は"
        "一切変更せず、そのまま出力すること\n"
        "- コメント内の技術用語 (CLI コマンド、YAML キー、API 名、製品名) は"
        "英語のまま残すこと\n"
        "- 出力はコードブロック全体 (コメント行を翻訳済み) とし、"
        "説明や注記は付けないこと\n\n"
        f"## コードブロック:\n{en_code}"
    )
    if current_ja:
        base += f"\n\n## 参考: 現在の日本語版:\n{current_ja}"
    return base


def _has_comments(lines: list[str]) -> bool:
    """コードブロック内にコメント行があるか判定する。

    フェンスマーカー (---- / ....) を除いた内部コンテンツのみを検査する。
    """
    _, _, content = _strip_code_fences(lines)
    for line in content:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            continue
        if _RE_COMMENT_LINE.match(stripped):
            return True
    return False


def _strip_list_prefix(lines: list[str]) -> tuple[str, list[str]]:
    """リスト項目の構造プレフィックスを分離して返す。"""
    if not lines:
        return "", lines
    first = lines[0]
    m = re.match(r"^(\*+\s+|\.+\s+)", first)
    if m:
        prefix = m.group(1)
        stripped_first = first[len(prefix):]
        return prefix, [stripped_first] + lines[1:]
    return "", lines


def _restore_list_prefix(prefix: str, lines: list[str]) -> list[str]:
    """翻訳後のテキストにリストプレフィックスを再付与する。"""
    if not prefix or not lines:
        return lines
    return [prefix + lines[0]] + lines[1:]


def _ensure_list_prefix(original_lines: list[str], translated_lines: list[str]) -> list[str]:
    """翻訳結果にリストプレフィックスが残っていることを保証する。"""
    if not original_lines or not translated_lines:
        return translated_lines
    m = re.match(r"^(\*+\s+|\.+\s+)", original_lines[0])
    if not m:
        return translated_lines
    prefix = m.group(1)
    if not re.match(r"^(\*+\s+|\.+\s+)", translated_lines[0]):
        translated_lines[0] = prefix + translated_lines[0]
    return translated_lines


def _strip_code_fences(
    lines: list[str],
) -> tuple[str, str, list[str]]:
    """コードブロックのフェンスマーカーを分離する。"""
    if len(lines) < 2:
        return "", "", lines
    first = lines[0].strip()
    last = lines[-1].strip()
    if (first.startswith("----") or first.startswith("....")) and (
        last.startswith("----") or last.startswith("....")
    ):
        return lines[0], lines[-1], lines[1:-1]
    return "", "", lines


def _restore_code_fences(
    opening: str, closing: str, lines: list[str]
) -> list[str]:
    """翻訳後のコンテンツにフェンスマーカーを再付与する。"""
    if not opening:
        return lines
    return [opening] + lines + [closing]


def _strip_admonition_prefix(lines: list[str]) -> tuple[str, list[str]]:
    """アドモニションキーワードプレフィックスを分離して返す。"""
    if not lines:
        return "", lines
    m = _RE_ADMONITION_PREFIX.match(lines[0])
    if m:
        keyword = m.group(1)
        rest = lines[0][m.end():]
        return keyword, [rest] + lines[1:]
    return "", lines


def _restore_admonition_prefix(keyword: str, lines: list[str]) -> list[str]:
    """翻訳後のテキストにアドモニションキーワードを再付与する。"""
    if not keyword or not lines:
        return lines
    return [f"{keyword}: {lines[0]}"] + lines[1:]


def _ensure_admonition_prefix(
    original_lines: list[str], translated_lines: list[str]
) -> list[str]:
    """翻訳結果のアドモニションキーワードが英語のまま残っていることを保証する。"""
    if not original_lines or not translated_lines:
        return translated_lines
    m = _RE_ADMONITION_PREFIX.match(original_lines[0])
    if not m:
        return translated_lines
    keyword = m.group(1)
    if _RE_ADMONITION_PREFIX.match(translated_lines[0]):
        return translated_lines
    for ja_kw in _TRANSLATED_ADMONITION_MAP:
        pat = f"{ja_kw}: "
        if translated_lines[0].startswith(pat):
            rest = translated_lines[0][len(pat):]
            translated_lines[0] = f"{keyword}: {rest}"
            return translated_lines
        pat2 = f"{ja_kw}:"
        if translated_lines[0].startswith(pat2) and (
            len(translated_lines[0]) == len(pat2)
            or translated_lines[0][len(pat2)] == " "
        ):
            rest = translated_lines[0][len(pat2):].lstrip()
            translated_lines[0] = f"{keyword}: {rest}"
            return translated_lines
    translated_lines[0] = f"{keyword}: {translated_lines[0]}"
    return translated_lines


def _ensure_heading_level(
    en_lines: list[str], ja_lines: list[str]
) -> list[str]:
    """翻訳結果の見出しレベルを EN と一致させる。"""
    if not en_lines or not ja_lines:
        return ja_lines
    en_m = re.match(r"^(={1,5})\s", en_lines[0])
    ja_m = re.match(r"^(={1,5})\s", ja_lines[0])
    if en_m and ja_m and en_m.group(1) != ja_m.group(1):
        ja_lines = [en_m.group(1) + ja_lines[0][len(ja_m.group(1)):]] + ja_lines[1:]
    return ja_lines


def _ensure_admonition_case(
    en_lines: list[str], ja_lines: list[str]
) -> list[str]:
    """EN が小文字アドモニション (Note: 等) の場合、JA の大文字変換を復元する。"""
    if not en_lines or not ja_lines:
        return ja_lines
    en_first = en_lines[0]
    ja_first = ja_lines[0]
    for lower_kw, upper_kw in [
        ("Note:", "NOTE:"), ("Tip:", "TIP:"), ("Important:", "IMPORTANT:"),
        ("Warning:", "WARNING:"), ("Caution:", "CAUTION:"),
    ]:
        if en_first.startswith(lower_kw) and ja_first.startswith(upper_kw):
            ja_lines = [lower_kw + ja_first[len(upper_kw):]] + ja_lines[1:]
            break
    return ja_lines


_RE_ATTR_LINE = re.compile(r"^:([^:]+):\s*(.*)")


def _ensure_doc_header_attrs(
    en_lines: list[str], ja_lines: list[str]
) -> list[str]:
    """document_header の属性行を EN 側と一致させる。

    EN にある属性は EN の値で上書き（:navtitle: 等は翻訳不要）。
    EN にあって JA にない属性は追加する。
    """
    if not en_lines or not ja_lines:
        return ja_lines

    en_attrs: dict[str, str] = {}
    for line in en_lines:
        m = _RE_ATTR_LINE.match(line)
        if m:
            en_attrs[m.group(1)] = line

    if not en_attrs:
        return ja_lines

    result: list[str] = []
    seen_attrs: set[str] = set()
    for line in ja_lines:
        m = _RE_ATTR_LINE.match(line)
        if m and m.group(1) in en_attrs:
            if m.group(1) not in seen_attrs:
                result.append(en_attrs[m.group(1)])
                seen_attrs.add(m.group(1))
        else:
            result.append(line)

    insert_pos = 1
    for line in result[1:]:
        if _RE_ATTR_LINE.match(line):
            insert_pos += 1
        else:
            break
    for attr_name, attr_line in en_attrs.items():
        if attr_name not in seen_attrs:
            result.insert(insert_pos, attr_line)
            insert_pos += 1

    return result


def _dedup_sections(
    new_contents: list[tuple[str, list[str]]],
    up_blocks: list,
) -> list[tuple[str, list[str]]]:
    """new_contents から重複セクションを除去する。

    upstream のセクション見出し数と比較し、JA 側に余剰セクションがあれば
    2 回目以降の同名見出しとその配下ブロックを削除する。
    """
    def _heading_text(lines: list[str]) -> str:
        if not lines:
            return ""
        return re.sub(r"^={1,5}\s+", "", lines[0]).strip()

    def _heading_level(lines: list[str]) -> int:
        if not lines:
            return 0
        m = re.match(r"^(={1,5})\s", lines[0])
        return len(m.group(1)) if m else 0

    up_h2_texts = []
    for b in up_blocks:
        if b.block_type == "section_header":
            lvl = _heading_level(list(b.lines))
            if lvl == 2:
                up_h2_texts.append(
                    re.sub(r"^={1,5}\s+", "", b.lines[0]).strip()
                    if b.lines else ""
                )

    seen: dict[str, int] = {}
    dup_indices: set[int] = set()
    for i, (btype, lines) in enumerate(new_contents):
        if btype == "section_header":
            lvl = _heading_level(lines)
            if lvl == 2:
                text = _heading_text(lines)
                if text in seen:
                    dup_start = i
                    dup_end = len(new_contents)
                    for j in range(i + 1, len(new_contents)):
                        jtype, jlines = new_contents[j]
                        if jtype == "section_header" and _heading_level(jlines) <= lvl:
                            dup_end = j
                            break
                    for k in range(dup_start, dup_end):
                        dup_indices.add(k)
                else:
                    seen[text] = i

    if not dup_indices:
        return new_contents

    result = [entry for i, entry in enumerate(new_contents) if i not in dup_indices]
    return result


def _apply_heading_glossary(lines: list[str]) -> list[str] | None:
    """見出しテキストが用語集に一致すれば翻訳済み行を返す。一致しなければ None。"""
    if not lines:
        return None
    line = lines[0]
    m = re.match(r"^(={2,5})\s+", line)
    if not m:
        return None
    prefix = m.group(0)
    text = line[len(prefix):].strip()
    anchor = ""
    anchor_m = re.search(r"\s*\[\[([^\]]+)\]\]\s*$", text)
    if anchor_m:
        anchor = " " + anchor_m.group(0).strip()
        text = text[: anchor_m.start()].strip()
    ja_text = _HEADING_GLOSSARY.get(text)
    if ja_text is None:
        return None
    return [f"{prefix}{ja_text}{anchor}"]


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
    gemini_info: tuple,
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

        # ---- immutable types (literal_block, block_attribute) ----
        if up_block.block_type in _IMMUTABLE_TYPES:
            new_contents.append((up_block.block_type, list(up_block.lines)))
            continue

        # ---- code_block: translate comments only ----
        if up_block.block_type == "code_block":
            opening, closing, content = _strip_code_fences(up_block.lines)
            if _has_comments(up_block.lines):
                cur_ja_code = None
                if snap_idx is not None:
                    ja_idx = match_map.get(snap_idx)
                    if ja_idx is not None and 0 <= ja_idx < len(ja_blocks):
                        _, _, ja_content = _strip_code_fences(
                            ja_blocks[ja_idx].lines
                        )
                        cur_ja_code = "\n".join(ja_content)
                prompt = _build_code_comment_prompt(
                    "\n".join(content), cur_ja_code
                )
                translated = _call_ai(gemini_info, prompt)
                if translated:
                    result = translated.strip().split("\n")
                    result = _restore_code_fences(opening, closing, result)
                    new_contents.append(("code_block", result))
                    print(
                        f"  TRANSLATED  {up_id}  "
                        "(code block comments translated)"
                    )
                    stats["blocks_translated"] += 1
                else:
                    new_contents.append(("code_block", list(up_block.lines)))
                    skipped.append((rel_path, up_id))
            else:
                new_contents.append(("code_block", list(up_block.lines)))
            continue

        # ---- section_header: apply heading glossary ----
        if up_block.block_type == "section_header":
            glossary_lines = _apply_heading_glossary(up_block.lines)
            if glossary_lines is not None:
                glossary_lines = _ensure_heading_level(
                    list(up_block.lines), glossary_lines
                )
                new_contents.append(("section_header", glossary_lines))
                if status in ("outdated", "new"):
                    print(f"  GLOSSARY    {up_id}  (heading from glossary)")
                    stats["blocks_translated"] += 1
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
                translated = _call_ai(gemini_info, prompt)
                if translated:
                    result_lines = translated.strip().split("\n")
                    if up_block.block_type == "list_item":
                        result_lines = _ensure_list_prefix(
                            up_block.lines, result_lines
                        )
                    if up_block.block_type == "admonition_inline":
                        result_lines = _ensure_admonition_prefix(
                            up_block.lines, result_lines
                        )
                    if up_block.block_type == "section_header":
                        result_lines = _ensure_heading_level(
                            list(up_block.lines), result_lines
                        )
                    if up_block.block_type == "prose":
                        result_lines = _ensure_admonition_case(
                            list(up_block.lines), result_lines
                        )
                    if up_block.block_type == "document_header":
                        result_lines = _ensure_doc_header_attrs(
                            list(up_block.lines), result_lines
                        )
                    new_contents.append(
                        (up_block.block_type, result_lines)
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
                    translated = _call_ai(gemini_info, prompt)
                    if translated:
                        result_lines = translated.strip().split("\n")
                        if up_block.block_type == "list_item":
                            result_lines = _ensure_list_prefix(
                                up_block.lines, result_lines
                            )
                        if up_block.block_type == "admonition_inline":
                            result_lines = _ensure_admonition_prefix(
                                up_block.lines, result_lines
                            )
                        if up_block.block_type == "section_header":
                            result_lines = _ensure_heading_level(
                                list(up_block.lines), result_lines
                            )
                        if up_block.block_type == "prose":
                            result_lines = _ensure_admonition_case(
                                list(up_block.lines), result_lines
                            )
                        if up_block.block_type == "document_header":
                            result_lines = _ensure_doc_header_attrs(
                                list(up_block.lines), result_lines
                            )
                        new_contents.append(
                            (
                                up_block.block_type,
                                result_lines,
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
                translated = _call_ai(gemini_info, prompt)
                if translated:
                    result_lines = translated.strip().split("\n")
                    result_lines = _ensure_heading_level(
                        list(up_block.lines), result_lines
                    )
                    new_contents.append(
                        (up_block.block_type, result_lines)
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
                    translated = _call_ai(gemini_info, prompt)
                    if translated:
                        result_lines = translated.strip().split("\n")
                        if up_block.block_type == "list_item":
                            result_lines = _ensure_list_prefix(
                                up_block.lines, result_lines
                            )
                        if up_block.block_type == "admonition_inline":
                            result_lines = _ensure_admonition_prefix(
                                up_block.lines, result_lines
                            )
                        if up_block.block_type == "section_header":
                            result_lines = _ensure_heading_level(
                                list(up_block.lines), result_lines
                            )
                        if up_block.block_type == "prose":
                            result_lines = _ensure_admonition_case(
                                list(up_block.lines), result_lines
                            )
                        if up_block.block_type == "document_header":
                            result_lines = _ensure_doc_header_attrs(
                                list(up_block.lines), result_lines
                            )
                        new_contents.append(
                            (
                                up_block.block_type,
                                result_lines,
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
        translated = _call_ai(gemini_info, prompt)
        if translated:
            result_lines = translated.strip().split("\n")
            if up_block.block_type == "list_item":
                result_lines = _ensure_list_prefix(
                    up_block.lines, result_lines
                )
            if up_block.block_type == "admonition_inline":
                result_lines = _ensure_admonition_prefix(
                    up_block.lines, result_lines
                )
            if up_block.block_type == "section_header":
                result_lines = _ensure_heading_level(
                    list(up_block.lines), result_lines
                )
            if up_block.block_type == "prose":
                result_lines = _ensure_admonition_case(
                    list(up_block.lines), result_lines
                )
            if up_block.block_type == "document_header":
                result_lines = _ensure_doc_header_attrs(
                    list(up_block.lines), result_lines
                )
            new_contents.append(
                (up_block.block_type, result_lines)
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

    # deduplicate sections before writing
    new_contents = _dedup_sections(new_contents, up_blocks)

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
    gemini_info: tuple,
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
    translatable = [b for b in blocks if b.block_type not in _IMMUTABLE_TYPES]
    total_t = len(translatable)
    short = os.path.basename(rel_path)
    print(f"  TRANSLATING {short} ({total_t} blocks)...", end="", flush=True)

    new_contents: list[tuple[str, list[str]]] = []
    translated_count = 0

    # Phase 1: handle non-batchable blocks immediately, collect batchable ones
    _BATCHABLE = frozenset({
        "prose", "list_item", "admonition_inline",
        "section_header", "table", "example_block",
        "block_title", "document_header", "attribute_entry",
    })
    pending: list[tuple[int, Block, str, str]] = []
    # (index_in_new_contents, block, stripped_text, prefix_or_keyword)

    for block in blocks:
        idx = len(new_contents)
        if block.block_type in _IMMUTABLE_TYPES:
            new_contents.append((block.block_type, list(block.lines)))
        elif block.block_type == "code_block":
            opening, closing, content = _strip_code_fences(block.lines)
            if _has_comments(block.lines):
                prompt = _build_code_comment_prompt("\n".join(content))
                translated = _call_ai(gemini_info, prompt)
                if translated:
                    result = translated.strip().split("\n")
                    result = _restore_code_fences(opening, closing, result)
                    new_contents.append(("code_block", result))
                    translated_count += 1
                else:
                    new_contents.append(("code_block", list(block.lines)))
                    skipped.append((rel_path, block.block_id))
            else:
                new_contents.append(("code_block", list(block.lines)))
            print(".", end="", flush=True)
        elif block.block_type == "section_header":
            glossary_lines = _apply_heading_glossary(block.lines)
            if glossary_lines is not None:
                new_contents.append(("section_header", glossary_lines))
                translated_count += 1
                print(".", end="", flush=True)
            else:
                new_contents.append(("section_header", list(block.lines)))
                pending.append(
                    (idx, block, "\n".join(block.lines), "")
                )
        elif block.block_type == "admonition_inline":
            keyword, content_lines = _strip_admonition_prefix(block.lines)
            new_contents.append(("admonition_inline", list(block.lines)))
            pending.append(
                (idx, block, "\n".join(content_lines), keyword)
            )
        elif block.block_type == "list_item":
            prefix, content_lines = _strip_list_prefix(block.lines)
            new_contents.append(("list_item", list(block.lines)))
            pending.append(
                (idx, block, "\n".join(content_lines), prefix)
            )
        elif block.block_type in _BATCHABLE:
            new_contents.append((block.block_type, list(block.lines)))
            pending.append(
                (idx, block, "\n".join(block.lines), "")
            )
        else:
            new_contents.append((block.block_type, list(block.lines)))
            pending.append(
                (idx, block, "\n".join(block.lines), "")
            )

    # Phase 2: batch translate pending blocks
    batch_size = _rate_state.batch_size
    base_rules = (
        "あなたは技術文書の翻訳者です。OpenShift Virtualization に関する"
        "英語ドキュメントを日本語に翻訳してください。\n\n"
        "## ルール\n"
        "- AsciiDoc の構文 (マークアップ、xref、コードブロック参照、リンク等)"
        " はそのまま維持すること\n"
        "- 技術用語 (CLI コマンド、YAML キー、API 名、製品名) は"
        "英語のまま残すこと\n"
        "- AsciiDoc のアドモニションキーワード "
        "(NOTE:, WARNING:, IMPORTANT:, TIP:, CAUTION:) は"
        "英語のまま維持すること。日本語に翻訳しないこと\n"
        "- インラインアドモニション (NOTE: テキスト) と"
        "ブロックアドモニション ([NOTE]\\n====) の形式を"
        "相互に変換しないこと\n"
        "- 翻訳文のみを出力し、説明や注記は付けないこと\n"
        "- 自然な日本語で、技術的に正確な翻訳を行うこと"
    )

    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start:batch_start + batch_size]
        texts = [text for _, _, text, _ in batch]
        results = _call_ai_batch(gemini_info, texts, base_rules)

        for (idx, block, _text, prefix), translated in zip(batch, results):
            if translated:
                result_lines = translated.strip().split("\n")
                if block.block_type == "list_item" and prefix:
                    result_lines = _restore_list_prefix(prefix, result_lines)
                    result_lines = _ensure_list_prefix(
                        block.lines, result_lines
                    )
                elif block.block_type == "admonition_inline" and prefix:
                    result_lines = _restore_admonition_prefix(
                        prefix, result_lines
                    )
                    result_lines = _ensure_admonition_prefix(
                        block.lines, result_lines
                    )
                if block.block_type == "section_header":
                    result_lines = _ensure_heading_level(
                        list(block.lines), result_lines
                    )
                if block.block_type == "prose":
                    result_lines = _ensure_admonition_case(
                        list(block.lines), result_lines
                    )
                if block.block_type == "document_header":
                    result_lines = _ensure_doc_header_attrs(
                        list(block.lines), result_lines
                    )
                new_contents[idx] = (block.block_type, result_lines)
                translated_count += 1
            else:
                skipped.append((rel_path, block.block_id))
        print("." * len(batch), end="", flush=True)

    print()  # newline after dots

    if not dry_run and new_contents:
        os.makedirs(os.path.dirname(rel_path), exist_ok=True)
        with open(rel_path, "w", encoding="utf-8") as fh:
            fh.write(_reconstruct_file(new_contents))

    print(f"NEW FILE    {rel_path} ({translated_count} blocks translated)")
    stats["new_files"] += 1


# ---------------------------------------------------------------------------
# Post-processing — fix admonition keywords and heading glossary in all files
# ---------------------------------------------------------------------------


_RE_JA_ADMONITION_PREFIX = re.compile(
    r"^(" + "|".join(re.escape(k) for k in _TRANSLATED_ADMONITION_MAP) + r"):\s"
)


def _fix_translated_admonition(lines: list[str]) -> list[str] | None:
    """prose ブロックの先頭が日本語アドモニションの場合、英語に復元する。

    修正があれば新しい行リストを返す。なければ None。
    """
    if not lines:
        return None
    m = _RE_JA_ADMONITION_PREFIX.match(lines[0])
    if not m:
        return None
    ja_kw = m.group(1)
    en_kw = _TRANSLATED_ADMONITION_MAP.get(ja_kw)
    if not en_kw:
        return None
    rest = lines[0][m.end():]
    return [f"{en_kw}: {rest}"] + lines[1:]


def _postprocess_admonitions_and_headings() -> int:
    """Walk all JA .adoc files, fix admonition keywords and heading glossary.

    For heading glossary, compares with the originals/ snapshot to find the
    English heading text and applies the glossary if it matches.

    Returns the number of files modified.
    """
    files_fixed = 0
    for adoc_path in sorted(glob.glob("modules/*/pages/*.adoc")):
        with open(adoc_path, encoding="utf-8") as fh:
            content = fh.read()
        blocks = parse_blocks(content)

        en_headers: list[list[str]] = []
        en_doc_header: list[str] = []
        snap_path = os.path.join(ORIGINALS_DIR, adoc_path)
        if os.path.exists(snap_path):
            with open(snap_path, encoding="utf-8") as fh:
                en_blocks = parse_blocks(fh.read())
            en_headers = [
                list(b.lines) for b in en_blocks
                if b.block_type == "section_header"
            ]
            for b in en_blocks:
                if b.block_type == "document_header":
                    en_doc_header = list(b.lines)
                    break

        _LOWER_ADMON_KWS = {
            "NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important",
            "WARNING": "Warning", "CAUTION": "Caution",
        }
        en_has_lower_admon: set[str] = set()
        if os.path.exists(snap_path):
            for b in en_blocks:
                if b.block_type == "prose" and b.lines:
                    for lower_kw in _LOWER_ADMON_KWS.values():
                        if b.lines[0].startswith(lower_kw + ":"):
                            en_has_lower_admon.add(lower_kw)

        changed = False
        new_contents: list[tuple[str, list[str]]] = []
        header_idx = 0
        doc_header_attrs: set[str] = set()

        for block in blocks:
            if block.block_type == "admonition_inline":
                fixed = _ensure_admonition_prefix(
                    block.lines, list(block.lines)
                )
                restored = False
                if fixed and en_has_lower_admon:
                    m = _RE_ADMONITION_PREFIX.match(fixed[0])
                    if m:
                        upper_kw = m.group(1)
                        lower_kw = _LOWER_ADMON_KWS.get(upper_kw)
                        if lower_kw and lower_kw in en_has_lower_admon:
                            fixed = [
                                lower_kw + ":" + fixed[0][len(upper_kw) + 1:]
                            ] + fixed[1:]
                            new_contents.append(("prose", fixed))
                            changed = True
                            restored = True
                            en_has_lower_admon.discard(lower_kw)
                if not restored:
                    if fixed != list(block.lines):
                        new_contents.append(("admonition_inline", fixed))
                        changed = True
                    else:
                        new_contents.append(
                            ("admonition_inline", list(block.lines))
                        )
            elif block.block_type == "prose":
                fixed = _fix_translated_admonition(block.lines)
                if fixed is not None:
                    m = _RE_ADMONITION_PREFIX.match(fixed[0]) if fixed else None
                    upper_kw = m.group(1) if m else None
                    lower_kw = (
                        _LOWER_ADMON_KWS.get(upper_kw)
                        if upper_kw else None
                    )
                    if lower_kw and lower_kw in en_has_lower_admon:
                        restored = [
                            lower_kw + ":" + fixed[0][len(upper_kw) + 1:]
                        ] + fixed[1:]
                        new_contents.append(("prose", restored))
                        changed = True
                        en_has_lower_admon.discard(lower_kw)
                    else:
                        new_contents.append(("admonition_inline", fixed))
                        changed = True
                else:
                    new_contents.append(("prose", list(block.lines)))
            elif block.block_type == "section_header":
                result = list(block.lines)
                if header_idx < len(en_headers):
                    result = _ensure_heading_level(
                        en_headers[header_idx], result
                    )
                glossary = _apply_heading_glossary(result)
                if glossary is not None and glossary != list(block.lines):
                    new_contents.append(("section_header", glossary))
                    changed = True
                elif header_idx < len(en_headers):
                    en_glossary = _apply_heading_glossary(
                        en_headers[header_idx]
                    )
                    if (
                        en_glossary is not None
                        and en_glossary != list(block.lines)
                    ):
                        new_contents.append(
                            ("section_header", en_glossary)
                        )
                        changed = True
                    elif result != list(block.lines):
                        new_contents.append(("section_header", result))
                        changed = True
                    else:
                        new_contents.append(
                            ("section_header", list(block.lines))
                        )
                else:
                    new_contents.append(
                        ("section_header", list(block.lines))
                    )
                header_idx += 1
            elif block.block_type == "document_header" and en_doc_header:
                fixed = _ensure_doc_header_attrs(
                    en_doc_header, list(block.lines)
                )
                doc_header_attrs = set()
                for line in fixed:
                    m = _RE_ATTR_LINE.match(line)
                    if m:
                        doc_header_attrs.add(m.group(1))
                if fixed != list(block.lines):
                    new_contents.append(("document_header", fixed))
                    changed = True
                else:
                    new_contents.append(
                        ("document_header", list(block.lines))
                    )
            elif block.block_type == "attribute_entry" and block.lines:
                m = _RE_ATTR_LINE.match(block.lines[0])
                if m and m.group(1) in doc_header_attrs:
                    changed = True
                else:
                    new_contents.append(
                        (block.block_type, list(block.lines))
                    )
            else:
                new_contents.append(
                    (block.block_type, list(block.lines))
                )

        if changed:
            corrected = sum(
                1
                for (_, new_lines), blk in zip(new_contents, blocks)
                if new_lines != list(blk.lines)
            )
            with open(adoc_path, "w", encoding="utf-8") as fh:
                fh.write(_reconstruct_file(new_contents))
            print(
                f"POSTFIX     {adoc_path} "
                f"({corrected} blocks corrected)"
            )
            files_fixed += 1

    return files_fixed


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
    gemini_info: tuple,
    stats: dict,
    skipped: list,
    dry_run: bool,
    completed_files: set | None = None,
    progress: dict | None = None,
) -> None:
    if not dry_run:
        for sub in ("pages", "attachments", "images"):
            os.makedirs(os.path.join(module_path, sub), exist_ok=True)

    # nav.adoc
    nav_path = os.path.join(module_path, "nav.adoc")
    up_nav = git_show(upstream_ref, nav_path)
    if up_nav:
        _translate_and_write_nav(
            nav_path, up_nav, gemini_info, skipped, dry_run
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
        if completed_files and page in completed_files:
            print(f"SKIP        {page} (already translated)")
            continue
        _translate_new_file(
            page, upstream_ref, upstream_commit, manifest,
            gemini_info, stats, skipped, dry_run,
        )
        if not dry_run and completed_files is not None and progress is not None:
            completed_files.add(page)
            progress["completed_files"] = sorted(completed_files)
            save_manifest(manifest)
            _save_progress(progress)

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
    gemini_info: tuple,
    skipped: list,
    dry_run: bool,
) -> None:
    """Translate display text of xref lines and write nav.adoc."""
    new_lines: list[str] = []
    for line in upstream_content.rstrip("\n").split("\n"):
        display = _extract_xref_display(line)
        target = _extract_xref_target(line)
        if target and display:
            translated = _call_ai(gemini_info, _build_new_file_prompt(display))
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
    gemini_info: tuple,
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
                    gemini_info, _build_new_file_prompt(display)
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
# antora-playbook.yml sync  (step 9a)
# ---------------------------------------------------------------------------


def _sync_antora_playbook_yml(
    upstream_ref: str,
    stats: dict,
    dry_run: bool,
) -> None:
    up_content = git_show(upstream_ref, "antora-playbook.yml")
    if not up_content:
        return

    ja_name: str | None = None
    if os.path.exists("antora.yml"):
        with open("antora.yml", encoding="utf-8") as fh:
            for raw_line in fh:
                if raw_line.strip().startswith("name:"):
                    ja_name = raw_line.strip().split(":", 1)[1].strip()
                    break
    if not ja_name:
        return

    lines = up_content.rstrip("\n").split("\n")
    result: list[str] = []
    build_date_found = False

    in_asciidoc = False
    in_attrs = False
    attr_indent = "    "
    last_attr_idx = -1

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else -1

        if stripped.startswith("start_page:") and "::" in stripped:
            comp = stripped.split(":", 1)[1].strip().split("::")[0]
            if comp != ja_name:
                line = line.replace(f"{comp}::", f"{ja_name}::")

        result.append(line)

        if indent == 0 and stripped:
            if stripped == "asciidoc:" or stripped.startswith("asciidoc:"):
                in_asciidoc = True
                in_attrs = False
            else:
                if in_attrs and not build_date_found:
                    result.insert(last_attr_idx + 1, f"{attr_indent}build-date: '@'")
                    build_date_found = True
                in_asciidoc = False
                in_attrs = False
            continue

        if in_asciidoc and not in_attrs and stripped.startswith("attributes:"):
            in_attrs = True
            continue

        if in_attrs and stripped and indent > 0:
            attr_indent = " " * indent
            last_attr_idx = len(result) - 1
            if stripped.startswith("build-date:"):
                if stripped != "build-date: '@'":
                    result[-1] = f"{attr_indent}build-date: '@'"
                build_date_found = True

    if in_attrs and not build_date_found:
        result.insert(last_attr_idx + 1, f"{attr_indent}build-date: '@'")

    new_content = "\n".join(result)
    if not new_content.endswith("\n"):
        new_content += "\n"

    current = ""
    if os.path.exists("antora-playbook.yml"):
        with open("antora-playbook.yml", encoding="utf-8") as fh:
            current = fh.read()

    if new_content != current:
        if not dry_run:
            with open("antora-playbook.yml", "w", encoding="utf-8") as fh:
                fh.write(new_content)
        print("PLAYBOOK    antora-playbook.yml: updated from upstream")
        stats["playbook_updated"] = True


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
        "--model",
        default=None,
        help="Gemini モデル (デフォルト: gemini-3.7-flash)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="作成するブランチ名 (デフォルト: translate/YYYY-MM-DD)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="中断した翻訳を再開する。既存のブランチに切り替え、"
        "翻訳済みファイルをスキップして続行する",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        help="処理対象のファイルまたはディレクトリ (省略時: 全管理ファイル)",
    )
    args = parser.parse_args()

    # ---- validate Gemini ----
    gemini_info = _validate_gemini(args.model)

    # ---- check manifest ----
    if not os.path.exists(MANIFEST_PATH):
        print(
            "Error: manifest.json not found. Run sync-init.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = load_manifest()

    # ---- install SIGINT handler ----
    if not args.dry_run:
        signal.signal(signal.SIGINT, _sigint_handler)

    # ---- resume or fresh start ----
    progress: dict = {}
    if args.resume:
        progress = _load_progress()
        if not progress:
            print(
                "Error: Progress file not found. "
                "The previous run may have completed successfully.",
                file=sys.stderr,
            )
            sys.exit(1)
        branch = progress["branch"]
        if not git_branch_exists(branch):
            print(
                f"Error: Branch '{branch}' not found. "
                "Run without --resume to start a new translation.",
                file=sys.stderr,
            )
            sys.exit(1)
        subprocess.run(
            ["git", "switch", branch],
            capture_output=True, check=True,
        )
        completed_count = len(progress.get("completed_files", []))
        print(
            f"RESUME      Resuming on branch '{branch}' "
            f"({completed_count} files already completed)"
        )
    else:
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
                    "Use --resume to continue, or --branch to specify "
                    "a different name.",
                    file=sys.stderr,
                )
                sys.exit(1)
            start = git_current_branch()
            git_switch_create_branch(branch, start)
            print(f"BRANCH      Created branch '{branch}' from {start}")

    # ---- step 3: inline sync-check ----
    if args.resume and progress.get("sync_check_done"):
        print("CHECKED     sync-check skipped (already done)")
        files_changed = 0
    else:
        files_changed = _sync_check_inline(manifest, UPSTREAM_REF)
        print(
            f"CHECKED     sync-check.py executed "
            f"(fetched upstream, {files_changed} files with changes)"
        )
        if not args.dry_run:
            progress = progress or _init_progress(branch, gemini_info[1])
            progress["sync_check_done"] = True
            _save_progress(progress)

    if not args.dry_run:
        save_manifest(manifest)
        if not progress:
            progress = _init_progress(branch, gemini_info[1])

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

    completed_files = set(progress.get("completed_files", []))
    completed_modules = set(progress.get("completed_modules", []))

    for mod in new_modules:
        if mod in completed_modules:
            print(f"SKIP        {mod}/ (already translated)")
            continue
        _create_new_module(
            mod, UPSTREAM_REF, upstream_commit, manifest,
            gemini_info, stats, skipped, args.dry_run,
            completed_files=completed_files,
            progress=progress,
        )
        if not args.dry_run:
            completed_modules.add(mod)
            progress["completed_modules"] = sorted(completed_modules)
            save_manifest(manifest)
            _save_progress(progress)

    for fpath in new_files:
        if fpath in completed_files:
            print(f"SKIP        {fpath} (already translated)")
            continue
        _translate_new_file(
            fpath, UPSTREAM_REF, upstream_commit, manifest,
            gemini_info, stats, skipped, args.dry_run,
        )
        if not args.dry_run:
            completed_files.add(fpath)
            progress["completed_files"] = sorted(completed_files)
            save_manifest(manifest)
            _save_progress(progress)

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
        if rel_path in completed_files:
            print(f"SKIP        {rel_path} (already translated)")
            continue
        _process_file(
            rel_path, file_entry, UPSTREAM_REF,
            gemini_info, stats, skipped, args.dry_run,
        )
        if not args.dry_run:
            completed_files.add(rel_path)
            progress["completed_files"] = sorted(completed_files)
            save_manifest(manifest)
            _save_progress(progress)
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
            mod, UPSTREAM_REF, gemini_info, stats, skipped, args.dry_run,
        )

    # ---- step 9: antora.yml sync ----
    _sync_antora_yml(UPSTREAM_REF, stats, args.dry_run)

    # ---- step 9a: antora-playbook.yml sync ----
    _sync_antora_playbook_yml(UPSTREAM_REF, stats, args.dry_run)

    # ---- step 10: format ----
    if args.format and not args.dry_run:
        for script in ("add-jp-lat-spaces.py", "convert-fullwidth-parens.py"):
            script_path = os.path.join("tools", "translation", script)
            if os.path.exists(script_path):
                subprocess.run(
                    [sys.executable, script_path], check=False,
                )
                print(f"FORMATTED   {script} applied")

    # ---- step 10a: postprocess admonition/heading fixes ----
    if not args.dry_run:
        fixed_count = _postprocess_admonitions_and_headings()
        if fixed_count:
            print(
                f"POSTFIX     {fixed_count} files corrected "
                "(admonition/heading glossary)"
            )

    # ---- step 11: sync-mark ----
    if skipped:
        print(f"\nSkipped blocks ({len(skipped)}):")
        for path, bid in skipped:
            print(f"  {path}: {bid}")
        print("sync-mark.py NOT executed due to skipped blocks")
    elif not args.dry_run:
        _sync_mark_inline(manifest, UPSTREAM_REF)
        save_manifest(manifest)
        _delete_progress()
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
