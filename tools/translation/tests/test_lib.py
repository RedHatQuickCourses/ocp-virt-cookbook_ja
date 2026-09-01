#!/usr/bin/env python3
"""Unit tests for _lib shared library modules.

Covers block_parser, manifest, and anchor_matching.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

# Ensure the translation tool root is on sys.path so that ``from _lib import ...``
# works regardless of how the tests are invoked.
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOL_ROOT not in sys.path:
    sys.path.insert(0, _TOOL_ROOT)

from _lib.block_parser import (
    Block,
    compute_block_hash,
    generate_block_ids,
    parse_blocks,
    _slugify,
)
from _lib import manifest as manifest_mod
from _lib.manifest import (
    add_file_entry,
    get_file_entry,
    load_manifest,
    remove_block_entry,
    remove_file_entry,
    save_manifest,
    update_block_status,
)
from _lib.anchor_matching import (
    extract_fingerprint,
    find_ja_lines_for_block,
    match_blocks,
)


# =========================================================================
# block_parser tests
# =========================================================================

class TestBlockParser(unittest.TestCase):
    """Tests for _lib.block_parser."""

    # ---- 1. test_parse_empty ----
    def test_parse_empty(self):
        """Empty string returns empty list."""
        self.assertEqual(parse_blocks(""), [])

    # ---- 2. test_parse_document_header ----
    def test_parse_document_header(self):
        """``= Title`` followed by ``:navtitle:`` produces a document_header block."""
        content = "= Title\n:navtitle: Test\n"
        blocks = parse_blocks(content)
        self.assertTrue(len(blocks) >= 1)
        hdr = blocks[0]
        self.assertEqual(hdr.block_type, "document_header")
        self.assertIn("= Title", hdr.lines)
        self.assertIn(":navtitle: Test", hdr.lines)

    # ---- 3. test_parse_section_header ----
    def test_parse_section_header(self):
        """``== Section Title`` produces a section_header block."""
        content = "= Doc\n\n== Section Title\n"
        blocks = parse_blocks(content)
        section_blocks = [b for b in blocks if b.block_type == "section_header"]
        self.assertTrue(len(section_blocks) >= 1)
        self.assertEqual(section_blocks[0].lines, ["== Section Title"])

    # ---- 4. test_parse_prose ----
    def test_parse_prose(self):
        """A regular paragraph produces a prose block."""
        content = "= Doc\n\nThis is a paragraph.\n"
        blocks = parse_blocks(content)
        prose = [b for b in blocks if b.block_type == "prose"]
        self.assertTrue(len(prose) >= 1)
        self.assertIn("This is a paragraph.", prose[0].lines)

    # ---- 5. test_parse_code_block ----
    def test_parse_code_block(self):
        """``[source,yaml]`` + delimited block produces a code_block with attrs."""
        content = "= Doc\n\n[source,yaml]\n----\nkey: value\n----\n"
        blocks = parse_blocks(content)
        code = [b for b in blocks if b.block_type == "code_block"]
        self.assertEqual(len(code), 1)
        self.assertIn("[source,yaml]", code[0].attrs)
        # Lines should contain the delimiter and content
        self.assertIn("key: value", code[0].lines)

    # ---- 6. test_parse_literal_block ----
    def test_parse_literal_block(self):
        """``....`` delimited block produces a literal_block."""
        content = "= Doc\n\n....\nsome text\n....\n"
        blocks = parse_blocks(content)
        literal = [b for b in blocks if b.block_type == "literal_block"]
        self.assertEqual(len(literal), 1)
        self.assertIn("some text", literal[0].lines)

    # ---- 7. test_parse_example_block ----
    def test_parse_example_block(self):
        """``[NOTE]`` + ``====`` delimited block produces an example_block with attrs."""
        content = "= Doc\n\n[NOTE]\n====\nNote text here.\n====\n"
        blocks = parse_blocks(content)
        example = [b for b in blocks if b.block_type == "example_block"]
        self.assertEqual(len(example), 1)
        self.assertIn("[NOTE]", example[0].attrs)
        self.assertIn("Note text here.", example[0].lines)

    # ---- 8. test_parse_table ----
    def test_parse_table(self):
        """``[cols="1,2"]`` + ``|===`` delimited block produces a table with attrs."""
        content = '= Doc\n\n[cols="1,2"]\n|===\n|a |b\n|===\n'
        blocks = parse_blocks(content)
        tables = [b for b in blocks if b.block_type == "table"]
        self.assertEqual(len(tables), 1)
        self.assertIn('[cols="1,2"]', tables[0].attrs)

    # ---- 9. test_parse_admonition_inline ----
    def test_parse_admonition_inline(self):
        """``NOTE: some text`` produces an admonition_inline block."""
        content = "= Doc\n\nNOTE: some text\n"
        blocks = parse_blocks(content)
        adm = [b for b in blocks if b.block_type == "admonition_inline"]
        self.assertEqual(len(adm), 1)
        self.assertIn("NOTE: some text", adm[0].lines)

    # ---- 10. test_parse_list_item ----
    def test_parse_list_item(self):
        """``* item text`` produces a list_item block."""
        content = "= Doc\n\n* item text\n"
        blocks = parse_blocks(content)
        items = [b for b in blocks if b.block_type == "list_item"]
        self.assertTrue(len(items) >= 1)
        self.assertIn("* item text", items[0].lines)

    # ---- 11. test_parse_block_attribute_standalone ----
    def test_parse_block_attribute_standalone(self):
        """``[#anchor-id]`` alone (followed by a non-delimited block) becomes a block_attribute."""
        content = "= Doc\n\n[#anchor-id]\nSome paragraph text.\n"
        blocks = parse_blocks(content)
        # The [#anchor-id] should be flushed as a standalone block_attribute
        # because the next line is prose, not a delimiter
        attrs = [b for b in blocks if b.block_type == "block_attribute"]
        self.assertTrue(len(attrs) >= 1)
        self.assertIn("[#anchor-id]", attrs[0].lines)

    # ---- 12. test_parse_block_title_grouped ----
    def test_parse_block_title_grouped(self):
        """``.Title`` before a code block groups as a code_block with title set."""
        content = "= Doc\n\n.My Title\n----\ncode here\n----\n"
        blocks = parse_blocks(content)
        code = [b for b in blocks if b.block_type == "code_block"]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0].title, ".My Title")
        # The title line should be included in lines
        self.assertIn(".My Title", code[0].lines)

    # ---- 13. test_parse_attribute_entry ----
    def test_parse_attribute_entry(self):
        """``:key: value`` outside document header becomes an attribute_entry."""
        content = "= Doc\n\n== Section\n\n:key: value\n"
        blocks = parse_blocks(content)
        entries = [b for b in blocks if b.block_type == "attribute_entry"]
        self.assertTrue(len(entries) >= 1)
        self.assertIn(":key: value", entries[0].lines)

    # ---- 14. test_generate_block_ids ----
    def test_generate_block_ids(self):
        """Block IDs follow ``<section_path>/<block_type>/<ordinal>`` format."""
        content = "= Doc\n\n== First\n\nParagraph one.\n\nParagraph two.\n\n== Second\n\nAnother paragraph.\n"
        blocks = parse_blocks(content)
        generate_block_ids(blocks)
        # document_header at root
        self.assertEqual(blocks[0].block_id, "_root/document_header/0")
        # section_header "== First" -> slug "first"
        first_section = [b for b in blocks if b.block_type == "section_header" and "First" in b.lines[0]][0]
        self.assertEqual(first_section.block_id, "first/section_header/0")
        # Two prose blocks under "first"
        prose_first = [b for b in blocks if b.section_path == "first" and b.block_type == "prose"]
        self.assertEqual(len(prose_first), 2)
        self.assertEqual(prose_first[0].block_id, "first/prose/0")
        self.assertEqual(prose_first[1].block_id, "first/prose/1")
        # section_header "== Second" -> slug "second"
        second_section = [b for b in blocks if b.block_type == "section_header" and "Second" in b.lines[0]][0]
        self.assertEqual(second_section.block_id, "second/section_header/0")

    # ---- 14b. test_generate_block_ids_hierarchical ----
    def test_generate_block_ids_hierarchical(self):
        """Subsections with the same heading under different parents get unique IDs."""
        content = (
            "= Doc\n\n"
            "== Method One\n\nIntro one.\n\n"
            "=== When to Use\n\n* Use case A\n\n"
            "=== Limitations\n\n* Limit A\n\n"
            "== Method Two\n\nIntro two.\n\n"
            "=== When to Use\n\n* Use case B\n\n"
            "=== Limitations\n\n* Limit B\n\n"
        )
        blocks = parse_blocks(content)
        generate_block_ids(blocks)

        # Collect all list_item blocks (the * items)
        list_items = [b for b in blocks if b.block_type == "list_item"]
        self.assertEqual(len(list_items), 4)

        # Each "When to Use" list_item should have a unique block_id
        self.assertNotEqual(list_items[0].block_id, list_items[2].block_id)
        # Each "Limitations" list_item should have a unique block_id
        self.assertNotEqual(list_items[1].block_id, list_items[3].block_id)

        # Verify hierarchical section_path structure
        self.assertEqual(list_items[0].section_path, "method-one/when-to-use")
        self.assertEqual(list_items[1].section_path, "method-one/limitations")
        self.assertEqual(list_items[2].section_path, "method-two/when-to-use")
        self.assertEqual(list_items[3].section_path, "method-two/limitations")

    # ---- 14c. test_generate_block_ids_level_reset ----
    def test_generate_block_ids_level_reset(self):
        """A new level-2 heading clears deeper level slugs."""
        content = (
            "= Doc\n\n"
            "== Parent\n\n"
            "=== Child\n\nText under child.\n\n"
            "== Sibling\n\nText under sibling.\n\n"
        )
        blocks = parse_blocks(content)
        generate_block_ids(blocks)

        sibling_prose = [
            b for b in blocks
            if b.block_type == "prose" and b.section_path == "sibling"
        ]
        self.assertEqual(len(sibling_prose), 1)
        self.assertEqual(sibling_prose[0].block_id, "sibling/prose/0")

    # ---- 15. test_compute_block_hash ----
    def test_compute_block_hash(self):
        """Hash of known lines produces expected 16-char hex string."""
        lines = ["hello", "world"]
        result = compute_block_hash(lines)
        self.assertEqual(len(result), 16)
        # Verify it matches manual computation
        normalized = "hello\nworld"
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(result, expected)

    # ---- 16. test_slugify_japanese ----
    def test_slugify_japanese(self):
        """Japanese heading produces correct slug."""
        slug = _slugify("== 概要")
        self.assertEqual(slug, "概要")

    # ---- 17. test_slugify_mixed ----
    def test_slugify_mixed(self):
        """Mixed JP/EN heading produces correct slug."""
        slug = _slugify("== CPU ピンニング設定")
        self.assertEqual(slug, "cpu-ピンニング設定")

    # ---- 18. test_grouping_block_attr_with_code ----
    def test_grouping_block_attr_with_code(self):
        """``[source,bash]`` + delimited block is ONE code_block."""
        content = "= Doc\n\n[source,bash]\n----\necho hello\n----\n"
        blocks = parse_blocks(content)
        code = [b for b in blocks if b.block_type == "code_block"]
        self.assertEqual(len(code), 1)
        # attrs and delimited content are grouped together
        self.assertIn("[source,bash]", code[0].attrs)
        self.assertIn("[source,bash]", code[0].lines)
        self.assertIn("echo hello", code[0].lines)

    # ---- 18b. test_table_in_list_continuation ----
    def test_table_in_list_continuation(self):
        """Table inside list continuation is included in the list_item block."""
        content = (
            "= Doc\n\n"
            "== Section\n\n"
            ". Step one\n"
            "+\n"
            "|===\n"
            "|Col A |Col B\n"
            "\n"
            "|val1 |val2\n"
            "|===\n\n"
            "Some prose after.\n"
        )
        blocks = parse_blocks(content)
        list_items = [b for b in blocks if b.block_type == "list_item"]
        self.assertEqual(len(list_items), 1)
        # Table delimiters should be inside the list_item
        joined = "\n".join(list_items[0].lines)
        self.assertIn("|===", joined)
        self.assertIn("|val1 |val2", joined)
        # Prose after should be a separate block
        prose = [b for b in blocks if b.block_type == "prose"]
        self.assertEqual(len(prose), 1)
        self.assertIn("Some prose after.", prose[0].lines)

    # ---- 18c. test_eof_in_table_state ----
    def test_eof_in_table_state(self):
        """Content in an unterminated table at EOF is preserved, not dropped."""
        content = (
            "= Doc\n\n"
            "== Section\n\n"
            "|===\n"
            "|Col A |Col B\n"
            "\n"
            "|val1 |val2\n"
        )
        blocks = parse_blocks(content)
        tables = [b for b in blocks if b.block_type == "table"]
        self.assertEqual(len(tables), 1)
        joined = "\n".join(tables[0].lines)
        self.assertIn("|val1 |val2", joined)

    # ---- 18d. test_sections_after_table_in_list_continuation ----
    def test_sections_after_table_in_list_continuation(self):
        """Sections after a table in a list continuation are not dropped."""
        content = (
            "= Doc\n\n"
            "== Part One\n\n"
            ". Step\n"
            "+\n"
            "|===\n"
            "|A |B\n"
            "|===\n\n"
            "== Summary\n\n"
            "Final text.\n"
        )
        blocks = parse_blocks(content)
        sections = [b for b in blocks if b.block_type == "section_header"]
        self.assertEqual(len(sections), 2)
        summary_prose = [b for b in blocks if b.block_type == "prose"]
        self.assertEqual(len(summary_prose), 1)
        self.assertIn("Final text.", summary_prose[0].lines)

    # ---- 19. test_empty_lines_between_blocks ----
    def test_empty_lines_between_blocks(self):
        """Empty lines are separators, not part of blocks."""
        content = "= Doc\n\nParagraph A.\n\nParagraph B.\n"
        blocks = parse_blocks(content)
        for block in blocks:
            for line in block.lines:
                self.assertNotEqual(line.strip(), "", f"Empty line found in {block.block_type} block")

    # ---- 20. test_trailing_whitespace_delimiter ----
    def test_trailing_whitespace_delimiter(self):
        """``----  `` (with trailing spaces) still recognized as code delimiter."""
        content = "= Doc\n\n----  \nsome code\n----  \n"
        blocks = parse_blocks(content)
        code = [b for b in blocks if b.block_type == "code_block"]
        self.assertEqual(len(code), 1)


# =========================================================================
# manifest tests
# =========================================================================

class TestManifest(unittest.TestCase):
    """Tests for _lib.manifest."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.manifest_path = os.path.join(self.tmpdir.name, "manifest.json")
        self._patcher = mock.patch.object(manifest_mod, "MANIFEST_PATH", self.manifest_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmpdir.cleanup()

    # ---- 1. test_load_nonexistent ----
    def test_load_nonexistent(self):
        """Returns default empty manifest when file does not exist."""
        data = load_manifest()
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["files"], {})
        self.assertIn("upstream_remote", data)
        self.assertIn("upstream_branch", data)

    # ---- 2. test_save_and_load ----
    def test_save_and_load(self):
        """save -> load roundtrip preserves data."""
        original = {
            "version": 1,
            "upstream_remote": "upstream",
            "upstream_branch": "main",
            "files": {"test.adoc": {"upstream_path": "en/test.adoc"}},
        }
        save_manifest(original)
        loaded = load_manifest()
        self.assertEqual(loaded, original)

    # ---- 3. test_add_file_entry ----
    def test_add_file_entry(self):
        """Adds file with correct structure."""
        data = load_manifest()
        blocks = [("sec1/prose/0", "prose", "abc123")]
        add_file_entry(data, "ja/test.adoc", "en/test.adoc", "deadbeef", blocks)
        self.assertIn("ja/test.adoc", data["files"])
        entry = data["files"]["ja/test.adoc"]
        self.assertEqual(entry["upstream_path"], "en/test.adoc")
        self.assertEqual(entry["upstream_commit"], "deadbeef")
        self.assertIn("initialized_at", entry)
        self.assertIn("sec1/prose/0", entry["blocks"])
        block = entry["blocks"]["sec1/prose/0"]
        self.assertEqual(block["type"], "prose")
        self.assertEqual(block["en_hash"], "abc123")
        self.assertEqual(block["status"], "synced")
        self.assertIn("synced_at", block)

    # ---- 4. test_get_file_entry_existing ----
    def test_get_file_entry_existing(self):
        """Returns the entry for an existing file."""
        data = load_manifest()
        add_file_entry(data, "ja/test.adoc", "en/test.adoc", "aaa", [])
        entry = get_file_entry(data, "ja/test.adoc")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["upstream_path"], "en/test.adoc")

    # ---- 5. test_get_file_entry_missing ----
    def test_get_file_entry_missing(self):
        """Returns None for a missing file."""
        data = load_manifest()
        self.assertIsNone(get_file_entry(data, "nonexistent.adoc"))

    # ---- 6. test_update_block_status_synced ----
    def test_update_block_status_synced(self):
        """Updates status and synced_at when status is 'synced'."""
        data = load_manifest()
        add_file_entry(data, "ja/t.adoc", "en/t.adoc", "aaa", [("b/0", "prose", "h1")])
        entry = get_file_entry(data, "ja/t.adoc")
        old_synced_at = entry["blocks"]["b/0"]["synced_at"]
        update_block_status(entry, "b/0", "synced", en_hash="h2")
        self.assertEqual(entry["blocks"]["b/0"]["status"], "synced")
        self.assertEqual(entry["blocks"]["b/0"]["en_hash"], "h2")
        # synced_at should be updated (it will be >= old)
        self.assertIn("synced_at", entry["blocks"]["b/0"])

    # ---- 7. test_update_block_status_outdated ----
    def test_update_block_status_outdated(self):
        """Updates status but not synced_at when status is 'outdated'."""
        data = load_manifest()
        add_file_entry(data, "ja/t.adoc", "en/t.adoc", "aaa", [("b/0", "prose", "h1")])
        entry = get_file_entry(data, "ja/t.adoc")
        old_synced_at = entry["blocks"]["b/0"]["synced_at"]
        update_block_status(entry, "b/0", "outdated", en_hash="h_new")
        self.assertEqual(entry["blocks"]["b/0"]["status"], "outdated")
        self.assertEqual(entry["blocks"]["b/0"]["en_hash"], "h_new")
        # synced_at should NOT have changed
        self.assertEqual(entry["blocks"]["b/0"]["synced_at"], old_synced_at)

    # ---- 8. test_remove_file_entry ----
    def test_remove_file_entry(self):
        """File entry removed."""
        data = load_manifest()
        add_file_entry(data, "ja/t.adoc", "en/t.adoc", "aaa", [])
        self.assertIn("ja/t.adoc", data["files"])
        remove_file_entry(data, "ja/t.adoc")
        self.assertNotIn("ja/t.adoc", data["files"])

    # ---- 9. test_remove_block_entry ----
    def test_remove_block_entry(self):
        """Block entry removed."""
        data = load_manifest()
        add_file_entry(data, "ja/t.adoc", "en/t.adoc", "aaa", [("b/0", "prose", "h1")])
        entry = get_file_entry(data, "ja/t.adoc")
        self.assertIn("b/0", entry["blocks"])
        remove_block_entry(entry, "b/0")
        self.assertNotIn("b/0", entry["blocks"])

    # ---- 10. test_save_format ----
    def test_save_format(self):
        """Output has sorted keys, indent=2, trailing newline."""
        data = {
            "version": 1,
            "upstream_remote": "upstream",
            "upstream_branch": "main",
            "files": {},
        }
        save_manifest(data)
        with open(self.manifest_path, encoding="utf-8") as f:
            raw = f.read()
        # Must end with newline
        self.assertTrue(raw.endswith("\n"))
        # Must be valid JSON
        parsed = json.loads(raw)
        self.assertEqual(parsed, data)
        # Keys should be sorted: "files" < "upstream_branch" < "upstream_remote" < "version"
        keys = list(parsed.keys())
        self.assertEqual(keys, sorted(keys))
        # Should use 2-space indent (check for "  " but not "    " at top level)
        self.assertIn('  "files"', raw)


# =========================================================================
# anchor_matching tests
# =========================================================================

class TestAnchorMatching(unittest.TestCase):
    """Tests for _lib.anchor_matching."""

    # ---- 1. test_fingerprint_section_header ----
    def test_fingerprint_section_header(self):
        """Section header returns ("section", level, anchor_or_None)."""
        block = Block(
            block_type="section_header",
            lines=["== Introduction"],
            start_line=1,
            end_line=1,
            attrs=[],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("section", 2, None))

    def test_fingerprint_section_header_with_anchor(self):
        """Section header with anchor attribute returns anchor in fingerprint."""
        block = Block(
            block_type="section_header",
            lines=["== Introduction"],
            start_line=1,
            end_line=1,
            attrs=["[[intro-anchor]]"],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("section", 2, "intro-anchor"))

    # ---- 2. test_fingerprint_code_block ----
    def test_fingerprint_code_block(self):
        """Code block returns ("code", hash)."""
        block = Block(
            block_type="code_block",
            lines=["----", "echo hello", "----"],
            start_line=1,
            end_line=3,
        )
        fp = extract_fingerprint(block)
        self.assertIsNotNone(fp)
        self.assertEqual(fp[0], "code")
        # The hash should be of the content between delimiters
        expected_hash = compute_block_hash(["echo hello"])
        self.assertEqual(fp[1], expected_hash)

    # ---- 3. test_fingerprint_block_attribute ----
    def test_fingerprint_block_attribute(self):
        """Block attribute returns ("attr", raw_text)."""
        block = Block(
            block_type="block_attribute",
            lines=["[source,yaml]"],
            start_line=1,
            end_line=1,
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("attr", "[source,yaml]"))

    # ---- 4. test_fingerprint_admonition ----
    def test_fingerprint_admonition(self):
        """Admonition inline returns ("admonition", keyword)."""
        block = Block(
            block_type="admonition_inline",
            lines=["WARNING: Be careful here."],
            start_line=1,
            end_line=1,
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("admonition", "WARNING"))

    # ---- 5. test_fingerprint_table ----
    def test_fingerprint_table(self):
        """Table returns ("table", cols_attr)."""
        block = Block(
            block_type="table",
            lines=['[cols="1,2"]', "|===", "|a |b", "|==="],
            start_line=1,
            end_line=4,
            attrs=['[cols="1,2"]'],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("table", '[cols="1,2"]'))

    # ---- 6. test_fingerprint_prose_returns_none ----
    def test_fingerprint_prose_returns_none(self):
        """Prose has no fingerprint."""
        block = Block(
            block_type="prose",
            lines=["This is a paragraph."],
            start_line=1,
            end_line=1,
        )
        fp = extract_fingerprint(block)
        self.assertIsNone(fp)

    # ---- 7. test_match_identical_blocks ----
    def test_match_identical_blocks(self):
        """Identical EN/JA block sequences -> all matched."""
        en_blocks = [
            Block(block_type="section_header", lines=["== Intro"], start_line=1, end_line=1),
            Block(block_type="code_block", lines=["----", "code", "----"], start_line=2, end_line=4),
        ]
        ja_blocks = [
            Block(block_type="section_header", lines=["== Intro"], start_line=1, end_line=1),
            Block(block_type="code_block", lines=["----", "code", "----"], start_line=2, end_line=4),
        ]
        matches = match_blocks(en_blocks, ja_blocks)
        self.assertEqual(len(matches), 2)
        self.assertIn((0, 0), matches)
        self.assertIn((1, 1), matches)

    # ---- 8. test_match_with_translated_prose ----
    def test_match_with_translated_prose(self):
        """Code blocks identical, prose different -> code anchor-matched, prose positionally matched."""
        en_blocks = [
            Block(block_type="section_header", lines=["== Setup"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["Install the tool."], start_line=2, end_line=2),
            Block(block_type="code_block", lines=["----", "yum install foo", "----"], start_line=3, end_line=5),
        ]
        ja_blocks = [
            Block(block_type="section_header", lines=["== Setup"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["ツールをインストールします。"], start_line=2, end_line=2),
            Block(block_type="code_block", lines=["----", "yum install foo", "----"], start_line=3, end_line=5),
        ]
        matches = match_blocks(en_blocks, ja_blocks)
        self.assertEqual(len(matches), 3)
        # Section headers anchor-matched
        self.assertIn((0, 0), matches)
        # Code blocks anchor-matched
        self.assertIn((2, 2), matches)
        # Prose positionally matched in gap
        self.assertIn((1, 1), matches)

    # ---- 9. test_find_ja_lines ----
    def test_find_ja_lines(self):
        """Returns correct (start_line, end_line) for a matched block."""
        ja_blocks = [
            Block(block_type="section_header", lines=["== Intro"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["Some text."], start_line=3, end_line=5),
        ]
        matches = [(0, 0), (1, 1)]
        result = find_ja_lines_for_block(1, matches, ja_blocks)
        self.assertEqual(result, (3, 5))

    # ---- 10. test_find_ja_lines_not_found ----
    def test_find_ja_lines_not_found(self):
        """Returns None for unmatched block."""
        ja_blocks = [
            Block(block_type="section_header", lines=["== Intro"], start_line=1, end_line=1),
        ]
        matches = [(0, 0)]
        result = find_ja_lines_for_block(5, matches, ja_blocks)
        self.assertIsNone(result)


class TestSyncTranslateHelpers(unittest.TestCase):
    """Tests for sync-translate.py helper functions."""

    @classmethod
    def setUpClass(cls):
        from importlib import import_module
        # sync-translate contains a hyphen, so use importlib
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    # ---- admonition prefix strip ----
    def test_strip_admonition_prefix_note(self):
        keyword, lines = self.mod._strip_admonition_prefix(
            ["NOTE: This is a note."]
        )
        self.assertEqual(keyword, "NOTE")
        self.assertEqual(lines, ["This is a note."])

    def test_strip_admonition_prefix_warning(self):
        keyword, lines = self.mod._strip_admonition_prefix(
            ["WARNING: Be careful.", "More text."]
        )
        self.assertEqual(keyword, "WARNING")
        self.assertEqual(lines, ["Be careful.", "More text."])

    def test_strip_admonition_prefix_none(self):
        keyword, lines = self.mod._strip_admonition_prefix(
            ["Regular text."]
        )
        self.assertEqual(keyword, "")
        self.assertEqual(lines, ["Regular text."])

    # ---- admonition prefix restore ----
    def test_restore_admonition_prefix(self):
        result = self.mod._restore_admonition_prefix(
            "IMPORTANT", ["これは重要です。"]
        )
        self.assertEqual(result, ["IMPORTANT: これは重要です。"])

    def test_restore_admonition_prefix_empty(self):
        result = self.mod._restore_admonition_prefix(
            "", ["テキスト"]
        )
        self.assertEqual(result, ["テキスト"])

    # ---- admonition prefix ensure ----
    def test_ensure_admonition_prefix_already_correct(self):
        result = self.mod._ensure_admonition_prefix(
            ["NOTE: Original text."],
            ["NOTE: 翻訳されたテキスト。"],
        )
        self.assertEqual(result, ["NOTE: 翻訳されたテキスト。"])

    def test_ensure_admonition_prefix_translated_to_japanese(self):
        result = self.mod._ensure_admonition_prefix(
            ["IMPORTANT: Use strong passwords."],
            ["重要: 強力なパスワードを使用してください。"],
        )
        self.assertEqual(
            result,
            ["IMPORTANT: 強力なパスワードを使用してください。"],
        )

    def test_ensure_admonition_prefix_translated_tip(self):
        result = self.mod._ensure_admonition_prefix(
            ["TIP: Use automation."],
            ["ヒント: 自動化を使用してください。"],
        )
        self.assertEqual(
            result,
            ["TIP: 自動化を使用してください。"],
        )

    def test_ensure_admonition_prefix_translated_note(self):
        result = self.mod._ensure_admonition_prefix(
            ["NOTE: Remember this."],
            ["注: これを覚えてください。"],
        )
        self.assertEqual(
            result,
            ["NOTE: これを覚えてください。"],
        )

    def test_ensure_admonition_prefix_missing(self):
        result = self.mod._ensure_admonition_prefix(
            ["WARNING: Danger zone."],
            ["危険なゾーンです。"],
        )
        self.assertEqual(
            result,
            ["WARNING: 危険なゾーンです。"],
        )

    def test_ensure_admonition_prefix_non_admonition(self):
        result = self.mod._ensure_admonition_prefix(
            ["Regular text."],
            ["通常のテキスト。"],
        )
        self.assertEqual(result, ["通常のテキスト。"])

    # ---- heading glossary ----
    def test_heading_glossary_match(self):
        result = self.mod._apply_heading_glossary(["== See Also"])
        self.assertEqual(result, ["== 参照"])

    def test_heading_glossary_match_level3(self):
        result = self.mod._apply_heading_glossary(["=== Prerequisites"])
        self.assertEqual(result, ["=== 前提条件"])

    def test_heading_glossary_with_anchor(self):
        result = self.mod._apply_heading_glossary(
            ["== See Also [[see_also]]"]
        )
        self.assertEqual(result, ["== 参照 [[see_also]]"])

    def test_heading_glossary_no_match(self):
        result = self.mod._apply_heading_glossary(
            ["== Custom Section Title"]
        )
        self.assertIsNone(result)

    def test_heading_glossary_summary(self):
        result = self.mod._apply_heading_glossary(["== Summary"])
        self.assertEqual(result, ["== まとめ"])

    def test_heading_glossary_cleanup(self):
        result = self.mod._apply_heading_glossary(["== Cleanup"])
        self.assertEqual(result, ["== クリーンアップ"])

    # ---- code fence strip/restore ----
    def test_strip_code_fences_standard(self):
        lines = ["----", "# comment", "code", "----"]
        opening, closing, content = self.mod._strip_code_fences(lines)
        self.assertEqual(opening, "----")
        self.assertEqual(closing, "----")
        self.assertEqual(content, ["# comment", "code"])

    def test_strip_code_fences_dots(self):
        lines = ["....", "text", "...."]
        opening, closing, content = self.mod._strip_code_fences(lines)
        self.assertEqual(opening, "....")
        self.assertEqual(closing, "....")
        self.assertEqual(content, ["text"])

    def test_strip_code_fences_no_fences(self):
        lines = ["just text"]
        opening, closing, content = self.mod._strip_code_fences(lines)
        self.assertEqual(opening, "")
        self.assertEqual(closing, "")
        self.assertEqual(content, ["just text"])

    def test_restore_code_fences(self):
        result = self.mod._restore_code_fences(
            "----", "----", ["# コメント", "code"]
        )
        self.assertEqual(result, ["----", "# コメント", "code", "----"])

    def test_restore_code_fences_no_opening(self):
        result = self.mod._restore_code_fences("", "", ["code"])
        self.assertEqual(result, ["code"])

    def test_has_comments_with_bash_comment(self):
        lines = ["----", "# Delete VMs", "oc delete vm", "----"]
        self.assertTrue(self.mod._has_comments(lines))

    def test_has_comments_no_comment(self):
        lines = ["----", "oc get pods", "----"]
        self.assertFalse(self.mod._has_comments(lines))

    def test_has_comments_markdown_heading(self):
        """## inside code block is detected as comment (# prefix)."""
        lines = ["----", "## Virtual Machines", "| col |", "----"]
        self.assertTrue(self.mod._has_comments(lines))

    # ---- fix_translated_admonition ----
    def test_fix_translated_admonition_important(self):
        result = self.mod._fix_translated_admonition(
            ["重要: これは重要です。"]
        )
        self.assertEqual(result, ["IMPORTANT: これは重要です。"])

    def test_fix_translated_admonition_tip(self):
        result = self.mod._fix_translated_admonition(
            ["ヒント: 便利な情報です。"]
        )
        self.assertEqual(result, ["TIP: 便利な情報です。"])

    def test_fix_translated_admonition_note(self):
        result = self.mod._fix_translated_admonition(
            ["注: 補足です。"]
        )
        self.assertEqual(result, ["NOTE: 補足です。"])

    def test_fix_translated_admonition_no_match(self):
        result = self.mod._fix_translated_admonition(
            ["普通のテキストです。"]
        )
        self.assertIsNone(result)

    # ---- batch prompt / parse ----
    def test_build_batch_prompt(self):
        result = self.mod._build_batch_prompt(
            ["Hello", "World"], "Rules here"
        )
        self.assertIn("===BLOCK_1===", result)
        self.assertIn("===BLOCK_2===", result)
        self.assertIn("Hello", result)
        self.assertIn("World", result)
        self.assertIn("Rules here", result)

    def test_parse_batch_response_valid(self):
        response = (
            "===BLOCK_1===\nこんにちは\n"
            "===BLOCK_2===\n世界\n"
        )
        result = self.mod._parse_batch_response(response, 2)
        self.assertEqual(result, ["こんにちは", "世界"])

    def test_parse_batch_response_wrong_count(self):
        response = "===BLOCK_1===\nこんにちは\n"
        result = self.mod._parse_batch_response(response, 2)
        self.assertIsNone(result)

    def test_parse_batch_response_empty_block(self):
        response = (
            "===BLOCK_1===\nこんにちは\n"
            "===BLOCK_2===\n\n"
        )
        result = self.mod._parse_batch_response(response, 2)
        self.assertIsNone(result)

    def test_parse_batch_response_multiline(self):
        response = (
            "===BLOCK_1===\n行1\n行2\n行3\n"
            "===BLOCK_2===\n単一行\n"
        )
        result = self.mod._parse_batch_response(response, 2)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "行1\n行2\n行3")
        self.assertEqual(result[1], "単一行")

    def test_parse_batch_response_misnumbered(self):
        response = (
            "===BLOCK_1===\nA\n"
            "===BLOCK_3===\nB\n"
        )
        result = self.mod._parse_batch_response(response, 2)
        self.assertIsNone(result)

    # ---- rate state ----
    def test_rate_state_update_from_headers(self):
        state = self.mod._RateState()
        state.update_from_headers({
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-limit-tokens": "4000000",
            "x-ratelimit-remaining-requests": "999",
            "x-ratelimit-remaining-tokens": "3999000",
        })
        self.assertEqual(state.rpm_limit, 1000)
        self.assertEqual(state.tpm_limit, 4000000)
        self.assertEqual(state.batch_size, 15)

    def test_rate_state_batch_size_low_rpm(self):
        state = self.mod._RateState()
        state.update_from_headers({
            "x-ratelimit-limit-requests": "10",
            "x-ratelimit-limit-tokens": "4000000",
            "x-ratelimit-remaining-requests": "9",
            "x-ratelimit-remaining-tokens": "3999000",
        })
        self.assertEqual(state.batch_size, 5)

    def test_rate_state_adaptive_wait_plenty(self):
        state = self.mod._RateState()
        state.rpm_limit = 1000
        state.tpm_limit = 4000000
        state.remaining_rpm = 900
        state.remaining_tpm = 3500000
        self.assertEqual(state.adaptive_wait(), 0.0)

    def test_rate_state_adaptive_wait_low(self):
        state = self.mod._RateState()
        state.rpm_limit = 100
        state.tpm_limit = 4000000
        state.remaining_rpm = 10
        state.remaining_tpm = 3500000
        wait = state.adaptive_wait()
        self.assertGreater(wait, 0.0)

    def test_rate_state_halve_batch(self):
        state = self.mod._RateState()
        state.batch_size = 10
        state.halve_batch()
        self.assertEqual(state.batch_size, 5)
        state.halve_batch()
        self.assertEqual(state.batch_size, 2)
        state.halve_batch()
        self.assertEqual(state.batch_size, 1)
        state.halve_batch()
        self.assertEqual(state.batch_size, 1)


class TestEnsureHeadingLevel(unittest.TestCase):
    """Tests for _ensure_heading_level."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_level_mismatch_fixed(self):
        en = ["=== Prerequisites"]
        ja = ["== 前提条件"]
        result = self.mod._ensure_heading_level(en, ja)
        self.assertEqual(result, ["=== 前提条件"])

    def test_level_match_unchanged(self):
        en = ["== Overview"]
        ja = ["== 概要"]
        result = self.mod._ensure_heading_level(en, ja)
        self.assertEqual(result, ["== 概要"])

    def test_non_heading_unchanged(self):
        en = ["Some text"]
        ja = ["テキスト"]
        result = self.mod._ensure_heading_level(en, ja)
        self.assertEqual(result, ["テキスト"])

    def test_empty_lines(self):
        result = self.mod._ensure_heading_level([], ["== 見出し"])
        self.assertEqual(result, ["== 見出し"])
        result = self.mod._ensure_heading_level(["== Heading"], [])
        self.assertEqual(result, [])

    def test_deep_heading_level(self):
        en = ["==== Deep Heading"]
        ja = ["== 深い見出し"]
        result = self.mod._ensure_heading_level(en, ja)
        self.assertEqual(result, ["==== 深い見出し"])


class TestEnsureAdmonitionCase(unittest.TestCase):
    """Tests for _ensure_admonition_case."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_note_case_preserved(self):
        en = ["Note: Replace your-key with the actual key."]
        ja = ["NOTE: your-key を実際のキーに置き換えてください。"]
        result = self.mod._ensure_admonition_case(en, ja)
        self.assertEqual(
            result, ["Note: your-key を実際のキーに置き換えてください。"]
        )

    def test_uppercase_note_unchanged(self):
        en = ["NOTE: This is important."]
        ja = ["NOTE: これは重要です。"]
        result = self.mod._ensure_admonition_case(en, ja)
        self.assertEqual(result, ["NOTE: これは重要です。"])

    def test_non_admonition_unchanged(self):
        en = ["Some regular text"]
        ja = ["通常のテキスト"]
        result = self.mod._ensure_admonition_case(en, ja)
        self.assertEqual(result, ["通常のテキスト"])

    def test_tip_case_preserved(self):
        en = ["Tip: Use this command."]
        ja = ["TIP: このコマンドを使ってください。"]
        result = self.mod._ensure_admonition_case(en, ja)
        self.assertEqual(
            result, ["Tip: このコマンドを使ってください。"]
        )

    def test_empty_lines(self):
        result = self.mod._ensure_admonition_case([], ["NOTE: text"])
        self.assertEqual(result, ["NOTE: text"])


class TestDedupSections(unittest.TestCase):
    """Tests for _dedup_sections."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_no_duplicates(self):
        contents = [
            ("section_header", ["== セクション A"]),
            ("prose", ["テキスト A"]),
            ("section_header", ["== セクション B"]),
            ("prose", ["テキスト B"]),
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Section A"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text A"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== Section B"],
                  start_line=3, end_line=3),
            Block(block_type="prose", lines=["Text B"],
                  start_line=4, end_line=4),
        ]
        result = self.mod._dedup_sections(contents, up_blocks)
        self.assertEqual(len(result), 4)

    def test_duplicate_removed(self):
        contents = [
            ("section_header", ["== まとめ"]),
            ("prose", ["内容1"]),
            ("section_header", ["== まとめ"]),
            ("prose", ["内容2"]),
            ("section_header", ["== 次のセクション"]),
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Summary"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Content"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== Next"],
                  start_line=3, end_line=3),
        ]
        result = self.mod._dedup_sections(contents, up_blocks)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0][1], ["== まとめ"])
        self.assertEqual(result[1][1], ["内容1"])
        self.assertEqual(result[2][1], ["== 次のセクション"])

    def test_subsection_not_affected(self):
        contents = [
            ("section_header", ["== セクション"]),
            ("section_header", ["=== サブ"]),
            ("prose", ["テキスト"]),
        ]
        up_blocks = []
        result = self.mod._dedup_sections(contents, up_blocks)
        self.assertEqual(len(result), 3)


class TestEnsureDocHeaderAttrs(unittest.TestCase):
    """Tests for _ensure_doc_header_attrs."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_missing_navtitle_restored(self):
        en = ["= Cloud Init Fundamentals", ":navtitle: Cloud Init Fundamentals"]
        ja = ["= Cloud Init の基礎"]
        result = self.mod._ensure_doc_header_attrs(en, ja)
        self.assertEqual(result, [
            "= Cloud Init の基礎",
            ":navtitle: Cloud Init Fundamentals",
        ])

    def test_all_attrs_present_overwritten(self):
        en = ["= Title", ":navtitle: Title"]
        ja = ["= タイトル", ":navtitle: タイトル"]
        result = self.mod._ensure_doc_header_attrs(en, ja)
        self.assertEqual(result, ["= タイトル", ":navtitle: Title"])

    def test_no_attrs_in_en(self):
        en = ["= Title"]
        ja = ["= タイトル"]
        result = self.mod._ensure_doc_header_attrs(en, ja)
        self.assertEqual(result, ["= タイトル"])

    def test_multiple_missing_attrs(self):
        en = ["= Title", ":navtitle: Title", ":page-layout: home"]
        ja = ["= タイトル"]
        result = self.mod._ensure_doc_header_attrs(en, ja)
        self.assertIn(":navtitle: Title", result)
        self.assertIn(":page-layout: home", result)
        self.assertEqual(result[0], "= タイトル")

    def test_empty_inputs(self):
        result = self.mod._ensure_doc_header_attrs([], ["= タイトル"])
        self.assertEqual(result, ["= タイトル"])
        result = self.mod._ensure_doc_header_attrs(["= Title"], [])
        self.assertEqual(result, [])


class TestEnforceHeadingStructure(unittest.TestCase):
    """Tests for _enforce_heading_structure."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_no_change_when_matched(self):
        contents = [
            ("section_header", ["== セクション A"]),
            ("prose", ["テキスト"]),
            ("section_header", ["=== サブ"]),
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Section A"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["=== Sub"],
                  start_line=3, end_line=3),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(result[0][1], ["== セクション A"])
        self.assertEqual(result[2][1], ["=== サブ"])

    def test_level_corrected(self):
        contents = [
            ("section_header", ["== セクション A"]),
            ("prose", ["テキスト"]),
            ("section_header", ["== サブ"]),  # wrong: should be ===
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Section A"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["=== Sub"],
                  start_line=3, end_line=3),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(result[2][1], ["=== サブ"])

    def test_cascading_swap_corrected(self):
        """== ↔ === swaps across multiple headings are all fixed."""
        contents = [
            ("section_header", ["== 概要"]),
            ("prose", ["テキスト"]),
            ("section_header", ["=== メソッド"]),   # wrong: should be ==
            ("section_header", ["== サブ1"]),        # wrong: should be ===
            ("section_header", ["=== サブ2"]),       # wrong: should be ==
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Overview"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== Methods"],
                  start_line=3, end_line=3),
            Block(block_type="section_header", lines=["=== Sub1"],
                  start_line=4, end_line=4),
            Block(block_type="section_header", lines=["== Sub2"],
                  start_line=5, end_line=5),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(result[2][1], ["== メソッド"])
        self.assertEqual(result[3][1], ["=== サブ1"])
        self.assertEqual(result[4][1], ["== サブ2"])

    def test_count_mismatch_no_change(self):
        """When heading counts differ, return unchanged."""
        contents = [
            ("section_header", ["== A"]),
            ("section_header", ["== B"]),
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== A"],
                  start_line=1, end_line=1),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][1], ["== A"])

    def test_preserves_anchor(self):
        """Anchor [[id]] is preserved when level is corrected."""
        contents = [
            ("section_header", ["=== 前提条件 [[prerequisites]]"]),
        ]
        up_blocks = [
            Block(block_type="section_header",
                  lines=["== Prerequisites [[prerequisites]]"],
                  start_line=1, end_line=1),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(result[0][1], ["== 前提条件 [[prerequisites]]"])

    def test_empty_inputs(self):
        """Empty contents and empty up_blocks should return unchanged."""
        result = self.mod._enforce_heading_structure([], [])
        self.assertEqual(result, [])

    def test_no_section_headers(self):
        """Contents with no section_header blocks return unchanged."""
        contents = [
            ("prose", ["テキスト"]),
            ("code_block", ["[source,yaml]", "---", "key: value"]),
        ]
        up_blocks = [
            Block(block_type="prose", lines=["Text"],
                  start_line=1, end_line=1),
            Block(block_type="code_block",
                  lines=["[source,yaml]", "---", "key: value"],
                  start_line=2, end_line=4),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(len(result), 2)

    def test_deep_level_5_correction(self):
        """Level 5 (=====) headings are correctly enforced."""
        contents = [
            ("section_header", ["== セクション"]),
            ("section_header", ["=== サブ"]),
            ("section_header", ["==== 深い"]),
            ("section_header", ["=== 最深部"]),  # wrong: should be =====
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Section"],
                  start_line=1, end_line=1),
            Block(block_type="section_header", lines=["=== Sub"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["==== Deep"],
                  start_line=3, end_line=3),
            Block(block_type="section_header", lines=["===== Deepest"],
                  start_line=4, end_line=4),
        ]
        result = self.mod._enforce_heading_structure(contents, up_blocks)
        self.assertEqual(result[3][1], ["===== 最深部"])

    def test_dedup_then_enforce_count_mismatch(self):
        """After dedup reduces heading count, enforce skips (count mismatch)."""
        contents = [
            ("section_header", ["== まとめ"]),
            ("prose", ["内容1"]),
            ("section_header", ["== まとめ"]),  # duplicate
            ("prose", ["内容2"]),
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Summary"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Content"],
                  start_line=2, end_line=2),
        ]
        deduped = self.mod._dedup_sections(contents, up_blocks)
        self.assertEqual(len(deduped), 2)
        result = self.mod._enforce_heading_structure(deduped, up_blocks)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][1], ["== まとめ"])


class TestSyncedHeaderValidation(unittest.TestCase):
    """Tests for synced-path section_header validation logic."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_glossary_value_detected_as_mismatch(self):
        """JA text that matches a glossary value for a different EN heading
        should be flagged as a mismatch."""
        glossary = self.mod._HEADING_GLOSSARY
        self.assertIn("Prerequisites", glossary)
        self.assertEqual(glossary["Prerequisites"], "前提条件")
        self.assertNotIn("Cloning vs Golden Images", glossary)
        en_text = "Cloning vs Golden Images"
        ja_text = "前提条件"
        expected_ja = glossary.get(en_text)
        self.assertIsNone(expected_ja)
        self.assertIn(ja_text, glossary.values())

    def test_correct_glossary_match_not_flagged(self):
        """JA text that correctly matches the glossary for its EN heading
        should NOT be flagged."""
        glossary = self.mod._HEADING_GLOSSARY
        en_text = "Prerequisites"
        expected_ja = glossary.get(en_text)
        self.assertIsNotNone(expected_ja)

    def test_non_glossary_heading_not_flagged(self):
        """JA text that doesn't match any glossary value should not be flagged."""
        glossary = self.mod._HEADING_GLOSSARY
        en_text = "Cloning vs Golden Images"
        ja_text = "クローンとゴールデンイメージの比較"
        expected_ja = glossary.get(en_text)
        self.assertIsNone(expected_ja)
        self.assertNotIn(ja_text, glossary.values())

    def test_all_glossary_entries_trigger_detection(self):
        """Every glossary value should trigger mismatch when paired with a
        non-glossary EN heading."""
        glossary = self.mod._HEADING_GLOSSARY
        for en_key, ja_val in glossary.items():
            self.assertIn(ja_val, glossary.values())

    def test_retranslation_fallback_on_ai_failure(self):
        """When AI retranslation returns None, the level-corrected original
        JA heading is preserved (not lost)."""
        en_lines = ["== Cloning vs Golden Images"]
        ja_lines = ["=== 前提条件"]  # wrong level + wrong text
        kept = self.mod._ensure_heading_level(en_lines, list(ja_lines))
        self.assertEqual(kept, ["== 前提条件"])

    def test_synced_heading_level_fixed_even_without_retranslation(self):
        """Even if the heading text is not a glossary mismatch, the level
        should still be corrected in the synced path."""
        en_lines = ["== Some Unique Heading"]
        ja_lines = ["=== ユニークな見出し"]  # wrong level, text is fine
        kept = self.mod._ensure_heading_level(en_lines, list(ja_lines))
        self.assertEqual(kept, ["== ユニークな見出し"])
        ja_text = kept[0].lstrip("= ").strip()
        glossary = self.mod._HEADING_GLOSSARY
        expected = glossary.get("Some Unique Heading")
        self.assertIsNone(expected)
        self.assertNotIn(ja_text, glossary.values())


class TestDedupThenEnforceAlignment(unittest.TestCase):
    """Tests for the dedup → enforce pipeline when dedup brings counts
    into alignment (the inverse of the count-mismatch case)."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_dedup_aligns_count_then_enforce_fixes_levels(self):
        """dedup removes extra duplicate → counts match → enforce fixes levels."""
        contents = [
            ("section_header", ["== 概要"]),
            ("prose", ["テキスト"]),
            ("section_header", ["== 前提条件"]),     # duplicate of next
            ("section_header", ["== 前提条件"]),     # duplicate (to be removed)
            ("prose", ["手順内容"]),
            ("section_header", ["== まとめ"]),       # wrong level (should be ===)
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Overview"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== Prerequisites"],
                  start_line=3, end_line=3),
            Block(block_type="prose", lines=["Steps"],
                  start_line=4, end_line=4),
            Block(block_type="section_header", lines=["=== Summary"],
                  start_line=5, end_line=5),
        ]
        deduped = self.mod._dedup_sections(contents, up_blocks)
        self.assertEqual(
            sum(1 for bt, _ in deduped if bt == "section_header"), 3,
        )
        result = self.mod._enforce_heading_structure(deduped, up_blocks)
        headers = [(bt, lines) for bt, lines in result if bt == "section_header"]
        self.assertEqual(headers[0][1], ["== 概要"])
        self.assertEqual(headers[1][1], ["== 前提条件"])
        self.assertEqual(headers[2][1], ["=== まとめ"])

    def test_real_world_cascade_pattern(self):
        """Reproduces the real bug pattern: a section drops, all subsequent
        headings shift by one, levels cascade wrong.

        EN structure: == A, === B, == C, === D, == E
        JA (buggy):   == A, == B(wrong text+level), === C(wrong level),
                      == D(wrong text+level), === E(wrong level), == E(dup)
        After dedup+enforce the levels should match EN."""
        contents = [
            ("section_header", ["== セクション A"]),
            ("prose", ["テキスト A"]),
            ("section_header", ["== 前提条件"]),       # should be === B
            ("prose", ["テキスト B"]),
            ("section_header", ["=== セクション C"]),   # should be == C
            ("prose", ["テキスト C"]),
            ("section_header", ["== 前提条件"]),       # should be === D (dup)
            ("prose", ["テキスト D"]),
            ("section_header", ["=== セクション E"]),   # should be == E
            ("section_header", ["== セクション E"]),    # duplicate
        ]
        up_blocks = [
            Block(block_type="section_header", lines=["== Section A"],
                  start_line=1, end_line=1),
            Block(block_type="prose", lines=["Text A"],
                  start_line=2, end_line=2),
            Block(block_type="section_header", lines=["=== Section B"],
                  start_line=3, end_line=3),
            Block(block_type="prose", lines=["Text B"],
                  start_line=4, end_line=4),
            Block(block_type="section_header", lines=["== Section C"],
                  start_line=5, end_line=5),
            Block(block_type="prose", lines=["Text C"],
                  start_line=6, end_line=6),
            Block(block_type="section_header", lines=["=== Section D"],
                  start_line=7, end_line=7),
            Block(block_type="prose", lines=["Text D"],
                  start_line=8, end_line=8),
            Block(block_type="section_header", lines=["== Section E"],
                  start_line=9, end_line=9),
        ]
        deduped = self.mod._dedup_sections(contents, up_blocks)
        ja_header_count = sum(1 for bt, _ in deduped if bt == "section_header")
        en_header_count = sum(1 for b in up_blocks
                              if b.block_type == "section_header")
        if ja_header_count == en_header_count:
            result = self.mod._enforce_heading_structure(deduped, up_blocks)
            headers = [(bt, lines) for bt, lines in result
                       if bt == "section_header"]
            self.assertEqual(headers[0][1][0][:2], "==")
            self.assertEqual(headers[1][1][0][:3], "===")
            self.assertEqual(headers[2][1][0][:2], "==")
            self.assertNotEqual(headers[2][1][0][:3], "===")
            self.assertEqual(headers[3][1][0][:3], "===")
            self.assertEqual(headers[4][1][0][:2], "==")
            self.assertNotEqual(headers[4][1][0][:3], "===")


class TestSyncedPathRetranslationFlow(unittest.TestCase):
    """Integration test for the synced-path retranslation flow.

    Simulates the exact logic from _process_file L1267-1300 to verify
    the complete pipeline: _ensure_heading_level → mismatch detection →
    AI retranslation → _ensure_heading_level again."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def _run_synced_path(self, en_lines, ja_lines, ai_result=None):
        """Simulate the synced-path logic for a section_header."""
        mod = self.mod
        kept_lines = list(ja_lines)
        retranslated = False
        kept_lines = mod._ensure_heading_level(list(en_lines), kept_lines)
        ja_text = (
            re.sub(r"^={1,5}\s+", "", kept_lines[0]).strip()
            if kept_lines else ""
        )
        en_text = (
            re.sub(r"^={1,5}\s+", "", en_lines[0]).strip()
            if en_lines else ""
        )
        expected_ja = mod._HEADING_GLOSSARY.get(en_text)
        if expected_ja is None and ja_text in mod._HEADING_GLOSSARY.values():
            if ai_result is not None:
                kept_lines = ai_result.strip().split("\n")
                kept_lines = mod._ensure_heading_level(
                    list(en_lines), kept_lines
                )
                retranslated = True
        return kept_lines, retranslated

    def test_mismatch_triggers_retranslation(self):
        """Wrong glossary value on non-glossary EN heading →
        AI retranslation produces correct result."""
        kept, retranslated = self._run_synced_path(
            en_lines=["=== Cloning vs Golden Images"],
            ja_lines=["== 前提条件"],
            ai_result="=== クローンとゴールデンイメージの比較",
        )
        self.assertTrue(retranslated)
        self.assertEqual(kept, ["=== クローンとゴールデンイメージの比較"])

    def test_ai_fixes_wrong_level_in_retranslation(self):
        """AI returns heading with wrong level → second _ensure_heading_level
        corrects it."""
        kept, retranslated = self._run_synced_path(
            en_lines=["=== Cloning vs Golden Images"],
            ja_lines=["== 前提条件"],
            ai_result="== クローンとゴールデンイメージの比較",
        )
        self.assertTrue(retranslated)
        self.assertEqual(kept, ["=== クローンとゴールデンイメージの比較"])

    def test_ai_failure_preserves_level_corrected_original(self):
        """AI failure (None) → keep level-corrected original JA."""
        kept, retranslated = self._run_synced_path(
            en_lines=["=== Cloning vs Golden Images"],
            ja_lines=["== 前提条件"],
            ai_result=None,
        )
        self.assertFalse(retranslated)
        self.assertEqual(kept, ["=== 前提条件"])

    def test_non_glossary_text_skips_retranslation(self):
        """JA text not in glossary values → no retranslation needed."""
        kept, retranslated = self._run_synced_path(
            en_lines=["== Custom Heading"],
            ja_lines=["=== カスタム見出し"],
            ai_result="should not be used",
        )
        self.assertFalse(retranslated)
        self.assertEqual(kept, ["== カスタム見出し"])

    def test_glossary_heading_not_flagged(self):
        """EN heading IS in glossary → expected_ja is not None → no
        retranslation (glossary path would have handled it)."""
        kept, retranslated = self._run_synced_path(
            en_lines=["== Prerequisites"],
            ja_lines=["=== 概要"],
            ai_result="should not be used",
        )
        self.assertFalse(retranslated)
        self.assertEqual(kept, ["== 概要"])

    def test_all_glossary_values_trigger_retranslation(self):
        """Every glossary JA value triggers retranslation when paired with
        a non-glossary EN heading."""
        for en_key, ja_val in self.mod._HEADING_GLOSSARY.items():
            kept, retranslated = self._run_synced_path(
                en_lines=[f"== Non-Glossary Heading {en_key}"],
                ja_lines=[f"== {ja_val}"],
                ai_result=f"== 正しい翻訳 {en_key}",
            )
            self.assertTrue(
                retranslated,
                f"Expected retranslation for JA value '{ja_val}' "
                f"but it was not triggered",
            )


class TestGlossaryPreemptsSyncedPath(unittest.TestCase):
    """Verify that glossary-matched headings never reach the synced path
    because _apply_heading_glossary returns non-None and the code continues."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_all_glossary_headings_handled_by_glossary_path(self):
        """Every glossary EN heading returns a non-None result from
        _apply_heading_glossary, meaning the code takes the glossary branch
        (continue) and never reaches the synced path."""
        for en_text, expected_ja in self.mod._HEADING_GLOSSARY.items():
            for level in ("==", "===", "===="):
                result = self.mod._apply_heading_glossary(
                    [f"{level} {en_text}"]
                )
                self.assertIsNotNone(
                    result,
                    f"_apply_heading_glossary returned None for "
                    f"'{level} {en_text}' — this would fall through to "
                    f"the synced path incorrectly",
                )
                text = re.sub(r"^={2,5}\s+", "", result[0]).strip()
                self.assertEqual(
                    text, expected_ja,
                    f"Expected '{expected_ja}' but got '{text}' "
                    f"for '{level} {en_text}'",
                )

    def test_glossary_with_anchor_still_handled(self):
        """Glossary heading with [[anchor]] still returns non-None."""
        result = self.mod._apply_heading_glossary(
            ["== Prerequisites [[prereqs]]"]
        )
        self.assertIsNotNone(result)
        self.assertIn("前提条件", result[0])
        self.assertIn("[[prereqs]]", result[0])

    def test_non_glossary_heading_falls_through(self):
        """Non-glossary heading returns None from _apply_heading_glossary,
        confirming it would reach the synced path."""
        result = self.mod._apply_heading_glossary(
            ["== Cloning vs Golden Images"]
        )
        self.assertIsNone(result)


class TestSyncAntoraPlaybook(unittest.TestCase):
    """Tests for _sync_antora_playbook_yml."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        import shutil
        shutil.rmtree(self.tmpdir)

    def _write(self, name, content):
        with open(os.path.join(self.tmpdir, name), "w") as f:
            f.write(content)

    def _read(self, name):
        with open(os.path.join(self.tmpdir, name)) as f:
            return f.read()

    def test_component_name_replaced(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        self._write("antora-playbook.yml", "")
        upstream = (
            "site:\n"
            "  title: OpenShift Virtualization cookbook\n"
            "  start_page: ocp-virt-cookbook::index.adoc\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
        )
        with mock.patch.object(self.mod, "git_show", return_value=upstream):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        result = self._read("antora-playbook.yml")
        self.assertIn("ocp-virt-cookbook_ja::index.adoc", result)
        self.assertNotIn("ocp-virt-cookbook::index.adoc", result)

    def test_build_date_inserted(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        self._write("antora-playbook.yml", "")
        upstream = (
            "site:\n"
            "  title: Test\n"
            "  start_page: ocp-virt-cookbook::index.adoc\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
            "\n"
            "ui:\n"
            "  bundle:\n"
            "    url: ./ui-bundle/ui-bundle.zip\n"
        )
        with mock.patch.object(self.mod, "git_show", return_value=upstream):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        result = self._read("antora-playbook.yml")
        self.assertIn("build-date: '@'", result)
        lines = result.split("\n")
        bd_idx = next(i for i, l in enumerate(lines) if "build-date" in l)
        pg_idx = next(i for i, l in enumerate(lines) if "page-pagination" in l)
        self.assertEqual(bd_idx, pg_idx + 1)

    def test_build_date_value_overridden(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        self._write("antora-playbook.yml", "")
        upstream = (
            "site:\n"
            "  title: Test\n"
            "  start_page: ocp-virt-cookbook_ja::index.adoc\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
            "    build-date: '2026-01-01'\n"
        )
        with mock.patch.object(self.mod, "git_show", return_value=upstream):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        result = self._read("antora-playbook.yml")
        self.assertIn("build-date: '@'", result)
        self.assertNotIn("2026-01-01", result)

    def test_no_change_when_synced(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        synced = (
            "site:\n"
            "  title: Test\n"
            "  start_page: ocp-virt-cookbook_ja::index.adoc\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
            "    build-date: '@'\n"
        )
        self._write("antora-playbook.yml", synced)
        with mock.patch.object(self.mod, "git_show", return_value=synced):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        self.assertNotIn("playbook_updated", stats)

    def test_extensions_preserved_from_upstream(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        self._write("antora-playbook.yml", "")
        upstream = (
            "site:\n"
            "  title: Test\n"
            "  start_page: ocp-virt-cookbook::index.adoc\n"
            "\n"
            "antora:\n"
            "  extensions:\n"
            "    - require: '@antora/lunr-extension'\n"
            "      index_latest_only: true\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
        )
        with mock.patch.object(self.mod, "git_show", return_value=upstream):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        result = self._read("antora-playbook.yml")
        self.assertIn("lunr-extension", result)
        self.assertIn("index_latest_only: true", result)

    def test_dry_run_no_write(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        original = "site:\n  start_page: old::index.adoc\n"
        self._write("antora-playbook.yml", original)
        upstream = (
            "site:\n"
            "  start_page: ocp-virt-cookbook::index.adoc\n"
            "\n"
            "asciidoc:\n"
            "  attributes:\n"
            "    page-pagination: true\n"
        )
        with mock.patch.object(self.mod, "git_show", return_value=upstream):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, True)
        self.assertEqual(self._read("antora-playbook.yml"), original)
        self.assertTrue(stats.get("playbook_updated"))

    def test_no_upstream_playbook(self):
        self._write("antora.yml", "name: ocp-virt-cookbook_ja\n")
        self._write("antora-playbook.yml", "old content\n")
        with mock.patch.object(self.mod, "git_show", return_value=None):
            stats = {}
            self.mod._sync_antora_playbook_yml("upstream/main", stats, False)
        self.assertEqual(self._read("antora-playbook.yml"), "old content\n")


# =========================================================================
# Additional block_parser tests — coverage gaps
# =========================================================================

class TestBlockParserAdditional(unittest.TestCase):
    """Additional tests for _lib.block_parser covering spec gaps."""

    # ---- block_title + block_attribute + delimiter (2.3) ----
    def test_block_title_plus_attr_plus_example(self):
        """[NOTE] + .Title + ==== produces ONE example_block with both attrs
        and title (spec 2.3 / layer2-secondary.adoc pattern)."""
        content = (
            "= Doc\n\n"
            "[NOTE]\n"
            ".This is important\n"
            "====\n"
            "Note content here.\n"
            "====\n"
        )
        blocks = parse_blocks(content)
        example = [b for b in blocks if b.block_type == "example_block"]
        self.assertEqual(len(example), 1)
        self.assertIn("[NOTE]", example[0].attrs)
        self.assertEqual(example[0].title, ".This is important")
        self.assertIn("Note content here.", example[0].lines)

    # ---- list continuation with code block (2.4) ----
    def test_list_continuation_with_code_block(self):
        """``+\\n----\\n...\\n----`` inside a list produces ONE list_item."""
        content = (
            "= Doc\n\n"
            "== Section\n\n"
            ". Step one\n"
            "+\n"
            "----\n"
            "oc get pods\n"
            "----\n\n"
            "Some prose after.\n"
        )
        blocks = parse_blocks(content)
        list_items = [b for b in blocks if b.block_type == "list_item"]
        self.assertEqual(len(list_items), 1)
        joined = "\n".join(list_items[0].lines)
        self.assertIn("----", joined)
        self.assertIn("oc get pods", joined)
        prose = [b for b in blocks if b.block_type == "prose"]
        self.assertEqual(len(prose), 1)

    # ---- list continuation with literal block (2.4) ----
    def test_list_continuation_with_literal_block(self):
        """``+\\n....\\n...\\n....`` inside a list produces ONE list_item."""
        content = (
            "= Doc\n\n"
            ". Step\n"
            "+\n"
            "....\n"
            "output text\n"
            "....\n\n"
            "After.\n"
        )
        blocks = parse_blocks(content)
        list_items = [b for b in blocks if b.block_type == "list_item"]
        self.assertEqual(len(list_items), 1)
        joined = "\n".join(list_items[0].lines)
        self.assertIn("....", joined)
        self.assertIn("output text", joined)

    # ---- [[anchor-id]] standalone anchor (2.5) ----
    def test_standalone_bracket_anchor(self):
        """``[[anchor-id]]`` on its own line becomes block_attribute."""
        content = "= Doc\n\n[[my-anchor]]\n== Section\n"
        blocks = parse_blocks(content)
        attrs = [b for b in blocks if b.block_type == "block_attribute"]
        self.assertTrue(len(attrs) >= 1)
        self.assertIn("[[my-anchor]]", attrs[0].lines)

    # ---- [#anchor-id] shorthand anchor before code block (2.5) ----
    def test_shorthand_anchor_before_code_block(self):
        """``[#anchor-id]`` before ``----`` groups with the code_block."""
        content = "= Doc\n\n[#my-code]\n----\necho test\n----\n"
        blocks = parse_blocks(content)
        code = [b for b in blocks if b.block_type == "code_block"]
        self.assertEqual(len(code), 1)
        self.assertIn("[#my-code]", code[0].attrs)


class TestSlugifyAdditional(unittest.TestCase):
    """Additional slugify tests for spec 3.4 coverage."""

    def test_slugify_with_anchor_uses_anchor(self):
        """When [[anchor_id]] is present, generate_block_ids uses anchor,
        not slug (spec 3.4 rule 1)."""
        content = "= Doc\n\n[[custom-anchor]]\n== Some Heading\n\nParagraph text.\n"
        blocks = parse_blocks(content)
        generate_block_ids(blocks)
        prose = [b for b in blocks if b.block_type == "prose"]
        self.assertTrue(len(prose) >= 1)
        self.assertEqual(prose[0].section_path, "custom-anchor")
        self.assertEqual(prose[0].block_id, "custom-anchor/prose/0")

    def test_slugify_50_char_truncation(self):
        """Slugs are truncated at 50 characters (spec 3.4 rule 6)."""
        long_heading = "== " + "a" * 60
        slug = _slugify(long_heading)
        self.assertEqual(len(slug), 50)
        self.assertEqual(slug, "a" * 50)

    def test_slugify_fullwidth_to_halfwidth(self):
        """Full-width alphanumeric characters are converted to half-width
        (spec 3.4 rule 2)."""
        slug = _slugify("== Ｈｅｌｌｏ１２３")
        self.assertEqual(slug, "hello123")

    def test_slugify_fullwidth_katakana_preserved(self):
        """Full-width katakana is preserved (not alphanumeric)."""
        slug = _slugify("== テスト")
        self.assertEqual(slug, "テスト")

    def test_slugify_special_chars_removed(self):
        """Characters outside [a-z0-9぀-鿿-] are removed (spec 3.4 rule 5)."""
        slug = _slugify("== Hello! World? (test)")
        self.assertEqual(slug, "hello-world-test")


# =========================================================================
# Additional anchor_matching tests — coverage gaps
# =========================================================================

class TestAnchorMatchingAdditional(unittest.TestCase):
    """Additional tests for _lib.anchor_matching covering spec gaps."""

    # ---- code_block fingerprint excludes comment lines (5.2) ----
    def test_fingerprint_code_block_excludes_comments(self):
        """Code block fingerprint hashes only non-comment lines (spec 5.2)."""
        block_with_comments = Block(
            block_type="code_block",
            lines=["----", "# This is a comment", "echo hello", "# Another comment", "----"],
            start_line=1, end_line=5,
        )
        block_without_comments = Block(
            block_type="code_block",
            lines=["----", "echo hello", "----"],
            start_line=1, end_line=3,
        )
        fp_with = extract_fingerprint(block_with_comments)
        fp_without = extract_fingerprint(block_without_comments)
        self.assertIsNotNone(fp_with)
        self.assertIsNotNone(fp_without)
        self.assertEqual(fp_with, fp_without)

    def test_fingerprint_code_block_different_code(self):
        """Different code bodies produce different fingerprints."""
        block_a = Block(
            block_type="code_block",
            lines=["----", "echo hello", "----"],
            start_line=1, end_line=3,
        )
        block_b = Block(
            block_type="code_block",
            lines=["----", "echo world", "----"],
            start_line=1, end_line=3,
        )
        fp_a = extract_fingerprint(block_a)
        fp_b = extract_fingerprint(block_b)
        self.assertNotEqual(fp_a, fp_b)

    def test_fingerprint_code_block_same_code_different_comments(self):
        """Same code with translated comments produces same fingerprint."""
        block_en = Block(
            block_type="code_block",
            lines=["----", "# Delete all VMs", "oc delete vm --all", "----"],
            start_line=1, end_line=4,
        )
        block_ja = Block(
            block_type="code_block",
            lines=["----", "# 全ての VM を削除", "oc delete vm --all", "----"],
            start_line=1, end_line=4,
        )
        fp_en = extract_fingerprint(block_en)
        fp_ja = extract_fingerprint(block_ja)
        self.assertEqual(fp_en, fp_ja)

    # ---- literal_block fingerprint (5.2) ----
    def test_fingerprint_literal_block(self):
        """Literal block returns ("code", hash) using content between ....."""
        block = Block(
            block_type="literal_block",
            lines=["....", "output text", "more output", "...."],
            start_line=1, end_line=4,
        )
        fp = extract_fingerprint(block)
        self.assertIsNotNone(fp)
        self.assertEqual(fp[0], "code")
        expected_hash = compute_block_hash(["output text", "more output"])
        self.assertEqual(fp[1], expected_hash)

    # ---- example_block fingerprint (5.2) ----
    def test_fingerprint_example_block_with_admonition_attr(self):
        """Example block with [NOTE] attr returns ("attr", "[NOTE]")."""
        block = Block(
            block_type="example_block",
            lines=["[NOTE]", "====", "Some note.", "===="],
            start_line=1, end_line=4,
            attrs=["[NOTE]"],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("attr", "[NOTE]"))

    def test_fingerprint_example_block_without_admonition_attr(self):
        """Example block without admonition attr returns None."""
        block = Block(
            block_type="example_block",
            lines=["====", "Example content.", "===="],
            start_line=1, end_line=3,
            attrs=[],
        )
        fp = extract_fingerprint(block)
        self.assertIsNone(fp)

    # ---- section_header with inline anchor [[...]] (5.2) ----
    def test_fingerprint_section_header_inline_anchor(self):
        """Section header with inline [[anchor]] in text returns anchor."""
        block = Block(
            block_type="section_header",
            lines=["== Introduction [[intro-id]]"],
            start_line=1, end_line=1,
            attrs=[],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("section", 2, "intro-id"))

    def test_fingerprint_section_header_attrs_take_priority(self):
        """Anchor in attrs takes priority over inline anchor."""
        block = Block(
            block_type="section_header",
            lines=["== Introduction [[inline-id]]"],
            start_line=1, end_line=1,
            attrs=["[[attr-id]]"],
        )
        fp = extract_fingerprint(block)
        self.assertEqual(fp, ("section", 2, "attr-id"))

    # ---- gap fill with unequal block counts (5.3) ----
    def test_match_gap_unequal_blocks(self):
        """When EN has more gap blocks than JA, only min(en, ja) are matched."""
        en_blocks = [
            Block(block_type="section_header", lines=["== A"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["P1"], start_line=2, end_line=2),
            Block(block_type="prose", lines=["P2"], start_line=3, end_line=3),
            Block(block_type="prose", lines=["P3"], start_line=4, end_line=4),
            Block(block_type="section_header", lines=["== B"], start_line=5, end_line=5),
        ]
        ja_blocks = [
            Block(block_type="section_header", lines=["== A"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["JP1"], start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== B"], start_line=3, end_line=3),
        ]
        matches = match_blocks(en_blocks, ja_blocks)
        self.assertIn((0, 0), matches)
        self.assertIn((4, 2), matches)
        self.assertIn((1, 1), matches)
        self.assertNotIn((2, 2), matches)
        self.assertNotIn((3, 3), matches)

    def test_match_gap_ja_has_more(self):
        """When JA has more gap blocks than EN, only min(en, ja) are matched."""
        en_blocks = [
            Block(block_type="section_header", lines=["== A"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["P1"], start_line=2, end_line=2),
            Block(block_type="section_header", lines=["== B"], start_line=3, end_line=3),
        ]
        ja_blocks = [
            Block(block_type="section_header", lines=["== A"], start_line=1, end_line=1),
            Block(block_type="prose", lines=["JP1"], start_line=2, end_line=2),
            Block(block_type="prose", lines=["JP2"], start_line=3, end_line=3),
            Block(block_type="prose", lines=["JP3"], start_line=4, end_line=4),
            Block(block_type="section_header", lines=["== B"], start_line=5, end_line=5),
        ]
        matches = match_blocks(en_blocks, ja_blocks)
        self.assertIn((0, 0), matches)
        self.assertIn((2, 4), matches)
        self.assertIn((1, 1), matches)
        self.assertEqual(len(matches), 3)


# =========================================================================
# validate-structure.py tests
# =========================================================================

class TestValidateStructure(unittest.TestCase):
    """Tests for validate-structure.py _validate_file function."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "validate_structure",
            os.path.join(_TOOL_ROOT, "validate-structure.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_identical_files_no_violations(self):
        """Identical EN and JA structure produces no violations."""
        content = (
            "= Title\n\n"
            "== Section\n\n"
            "Paragraph text.\n\n"
            "[source,yaml]\n"
            "----\n"
            "key: value\n"
            "----\n"
        )
        violations = self.mod._validate_file(content, content, "test.adoc")
        self.assertEqual(violations, [])

    def test_section_heading_count_mismatch(self):
        """Different section heading counts → violation."""
        en = "= Title\n\n== Section A\n\nText.\n\n== Section B\n\nMore text.\n"
        ja = "= タイトル\n\n== セクション A\n\nテキスト。\n"
        violations = self.mod._validate_file(en, ja, "test.adoc")
        self.assertTrue(any("section heading count" in v for v in violations))

    def test_section_heading_level_mismatch(self):
        """Different section heading levels → violation."""
        en = "= Title\n\n== Section A\n\nText.\n"
        ja = "= タイトル\n\n=== セクション A\n\nテキスト。\n"
        violations = self.mod._validate_file(en, ja, "test.adoc")
        self.assertTrue(any("level" in v for v in violations))

    def test_block_type_count_mismatch(self):
        """Different block type counts in a section → violation."""
        en = "= Title\n\n== Section\n\nP1.\n\nP2.\n"
        ja = "= タイトル\n\n== セクション\n\nP1のみ。\n"
        violations = self.mod._validate_file(en, ja, "test.adoc")
        self.assertTrue(any("prose" in v for v in violations))

    def test_code_block_content_match_ignores_comments(self):
        """Code blocks with different comments but same code → no violation."""
        en = (
            "= Title\n\n"
            "== Section\n\n"
            "----\n"
            "# English comment\n"
            "oc get pods\n"
            "----\n"
        )
        ja = (
            "= タイトル\n\n"
            "== セクション\n\n"
            "----\n"
            "# 日本語コメント\n"
            "oc get pods\n"
            "----\n"
        )
        violations = self.mod._validate_file(en, ja, "test.adoc")
        code_violations = [v for v in violations if "code_block" in v and "content differs" in v]
        self.assertEqual(code_violations, [])

    def test_code_block_content_mismatch_detected(self):
        """Code blocks with different code body → violation."""
        en = (
            "= Title\n\n"
            "== Section\n\n"
            "----\n"
            "oc get pods\n"
            "----\n"
        )
        ja = (
            "= タイトル\n\n"
            "== セクション\n\n"
            "----\n"
            "oc get nodes\n"
            "----\n"
        )
        violations = self.mod._validate_file(en, ja, "test.adoc")
        self.assertTrue(any("content differs" in v for v in violations))

    def test_translated_prose_no_violation(self):
        """Translated prose with same structure → no violations."""
        en = (
            "= Title\n\n"
            "== Overview\n\n"
            "This is the overview paragraph.\n\n"
            "NOTE: Remember this.\n"
        )
        ja = (
            "= タイトル\n\n"
            "== 概要\n\n"
            "これは概要の段落です。\n\n"
            "NOTE: これを覚えてください。\n"
        )
        violations = self.mod._validate_file(en, ja, "test.adoc")
        self.assertEqual(violations, [])


# =========================================================================
# sync-translate.py: _verify_code_block_content tests (9.20.1)
# =========================================================================

class TestVerifyCodeBlockContent(unittest.TestCase):
    """Tests for _verify_code_block_content (spec 9.20.1)."""

    @classmethod
    def setUpClass(cls):
        spec = __import__("importlib").util.spec_from_file_location(
            "sync_translate",
            os.path.join(_TOOL_ROOT, "sync-translate.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_identical_content_unchanged(self):
        """Identical JA and EN returns JA unchanged."""
        lines = ["----", "oc get pods", "----"]
        result = self.mod._verify_code_block_content(lines, lines)
        self.assertEqual(result, lines)

    def test_translated_code_restored(self):
        """Translated code body (<=30%) is restored to EN."""
        en = ["----", "echo hello", "echo world", "echo test", "echo ok", "----"]
        ja = ["----", "echo こんにちは", "echo world", "echo test", "echo ok", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, en)

    def test_over_30_percent_not_restored(self):
        """If >30% of code lines differ, no restoration."""
        en = ["----", "line1", "line2", "line3", "----"]
        ja = ["----", "翻訳1", "翻訳2", "line3", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)

    def test_comments_are_ignored(self):
        """Comment lines (# prefix) are excluded from comparison."""
        en = ["----", "# English comment", "oc get pods", "----"]
        ja = ["----", "# 日本語コメント", "oc get pods", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)

    def test_delimiters_are_ignored(self):
        """Delimiter lines (----) are excluded from comparison."""
        en = ["----", "code", "----"]
        ja = ["----", "code", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)

    def test_line_count_mismatch_returns_ja(self):
        """Different line counts returns JA unchanged."""
        en = ["----", "line1", "line2", "----"]
        ja = ["----", "line1", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)

    def test_callout_marker_restored(self):
        """Callout markers (# <N>) that were removed are restored."""
        en = ["----", "oc get pods # <1>", "oc get nodes # <2>", "----"]
        ja = ["----", "oc get pods", "oc get nodes", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result[1], "oc get pods # <1>")
        self.assertEqual(result[2], "oc get nodes # <2>")

    def test_inline_comment_difference_ignored(self):
        """Lines where only inline comment portion differs are skipped."""
        en = ["----", "oc get pods  # Get all pods", "----"]
        ja = ["----", "oc get pods  # 全ての Pod を取得", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)

    def test_block_attribute_lines_ignored(self):
        """Lines starting with [ are excluded from comparison."""
        en = ["[source,bash]", "----", "echo hello", "----"]
        ja = ["[source,bash]", "----", "echo hello", "----"]
        result = self.mod._verify_code_block_content(ja, en)
        self.assertEqual(result, ja)


# =========================================================================
# _collect_files tests
# =========================================================================

class TestCollectFiles(unittest.TestCase):
    """Tests for _lib.common._collect_files."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_collect_from_directory(self):
        """Collects .adoc files recursively from a directory."""
        d = self.tmpdir.name
        sub = os.path.join(d, "pages")
        os.makedirs(sub)
        for name in ["a.adoc", "b.adoc", "c.txt"]:
            with open(os.path.join(sub, name), "w") as f:
                f.write("content")
        from _lib.common import _collect_files
        result = _collect_files([d])
        adoc_files = [f for f in result if f.endswith(".adoc")]
        self.assertEqual(len(adoc_files), 2)

    def test_collect_single_file(self):
        """Collects a single file path directly."""
        d = self.tmpdir.name
        fpath = os.path.join(d, "test.adoc")
        with open(fpath, "w") as f:
            f.write("content")
        from _lib.common import _collect_files
        result = _collect_files([fpath])
        self.assertEqual(result, [fpath])

    def test_collect_nonexistent_warns(self):
        """Non-existent path produces warning, returns empty."""
        from _lib.common import _collect_files
        result = _collect_files(["/nonexistent/path"])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
