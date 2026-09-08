#!/usr/bin/env python3
"""Unit tests for the pure helpers in sw-action.py, and for examples/.

Everything here runs offline: no server binary, no network. The server-backed
actions are covered by contract-check.py instead.

    python3 scripts/test-sw-action.py
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("sw_action", ROOT / "scripts" / "sw-action.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)


def load_jsonc(path):
    """Zed accepts // comments in its config files; json does not."""
    return json.loads(re.sub(r"^\s*//.*$", "", (ROOT / path).read_text(), flags=re.M))


class UriRoundTrip(unittest.TestCase):
    """urlparse is wrong here: it splits on # and ?, which are legal in paths."""

    def test_path_with_fragment_and_query_characters(self):
        path = "/tmp/a b/c#d?e/Resources/views/base.html.twig"
        self.assertEqual(sw.uri_to_path(sw.path_to_uri(path)), path)

    def test_non_ascii_path(self):
        path = "/tmp/größe/übersicht.twig"
        self.assertEqual(sw.uri_to_path(sw.path_to_uri(path)), path)

    def test_slashes_stay_unescaped(self):
        self.assertEqual(sw.path_to_uri("/a/b"), "file:///a/b")


class Utf16Columns(unittest.TestCase):
    """LSP counts UTF-16 code units; Python counts characters. Emoji differ."""

    LINE = '<div data-id="😀"></div>'

    def test_ascii_prefix_is_one_to_one(self):
        self.assertEqual(sw.utf16_to_index(self.LINE, 5), 5)
        self.assertEqual(sw.index_to_utf16(self.LINE, 5), 5)

    def test_astral_character_counts_as_two_units(self):
        emoji = self.LINE.index("😀")
        self.assertEqual(sw.index_to_utf16(self.LINE, emoji + 1), emoji + 2)

    def test_round_trip_across_the_astral_character(self):
        for index in range(len(self.LINE) + 1):
            units = sw.index_to_utf16(self.LINE, index)
            self.assertEqual(sw.utf16_to_index(self.LINE, units), index)

    def test_a_unit_inside_the_surrogate_pair_does_not_split_it(self):
        emoji = self.LINE.index("😀")
        inside = sw.index_to_utf16(self.LINE, emoji) + 1
        # Snapping forward keeps the pair intact; splitting it would write a
        # lone surrogate into the file.
        self.assertEqual(sw.utf16_to_index(self.LINE, inside), emoji + 1)


class ByteColumns(unittest.TestCase):
    """$ZED_COLUMN is a UTF-8 byte offset, which is not the UTF-16 one above."""

    LINE = '<div data-id="😀"></div>'

    def test_ascii_prefix_is_one_to_one(self):
        self.assertEqual(sw.byte_column_to_index(self.LINE, 5), 5)

    def test_the_two_encodings_disagree_after_an_astral_character(self):
        emoji = self.LINE.index("😀")
        after = emoji + 1
        # 4 bytes but 2 UTF-16 units: reading one as the other lands 2 short.
        self.assertEqual(sw.byte_column_to_index(self.LINE, emoji + 4), after)
        self.assertEqual(sw.utf16_to_index(self.LINE, emoji + 4), after + 2)

    def test_round_trip_over_every_character_boundary(self):
        for index in range(len(self.LINE) + 1):
            offset = len(self.LINE[:index].encode("utf-8"))
            self.assertEqual(sw.byte_column_to_index(self.LINE, offset), index)

    def test_offset_inside_a_sequence_snaps_forward(self):
        emoji = self.LINE.index("😀")
        start = len(self.LINE[:emoji].encode("utf-8"))
        for inside in range(start + 1, start + 4):
            self.assertEqual(sw.byte_column_to_index(self.LINE, inside), emoji + 1)

    def test_offsets_out_of_range_clamp(self):
        self.assertEqual(sw.byte_column_to_index(self.LINE, -5), 0)
        self.assertEqual(sw.byte_column_to_index(self.LINE, 9999), len(self.LINE))


class InsertUuid(unittest.TestCase):
    """run_uuid end to end, against real files in a temporary directory."""

    HEX = r"[0-9a-f]{32}"

    def insert(self, contents, row, column=1, print_only=False):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = pathlib.Path(directory) / "buffer.twig"
        path.write_text(contents, encoding="utf-8")
        args = argparse.Namespace(
            target=str(path), row=row, column=column,
            print_only=print_only, root=directory,
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sw.run_uuid(args, None)
        return path.read_text(encoding="utf-8"), stdout.getvalue()

    def test_inserts_on_the_final_blank_line(self):
        # splitlines drops the empty segment after a trailing newline. Without
        # it the row clamp walks back a line and appends to "hello" instead.
        result, _ = self.insert("hello\n", row=2)
        self.assertRegex(result, r"^hello\n" + self.HEX + r"$")

    def test_inserts_at_the_start_of_the_first_line(self):
        result, _ = self.insert("hello\n", row=1)
        self.assertRegex(result, r"^" + self.HEX + r"hello\n$")

    def test_inserts_mid_line_without_disturbing_the_rest(self):
        result, _ = self.insert("<span></span>\n", row=1, column=7)
        self.assertRegex(result, r"^<span>" + self.HEX + r"</span>\n$")

    def test_byte_column_lands_after_an_astral_character(self):
        # `<div data-id="` is 14 bytes, the emoji is 4, so Zed reports 19.
        result, _ = self.insert('<div data-id="😀"></div>\n', row=1, column=19)
        self.assertRegex(result, r'^<div data-id="😀' + self.HEX + r'"></div>\n$')

    def test_empty_file(self):
        result, _ = self.insert("", row=1)
        self.assertRegex(result, r"^" + self.HEX + r"$")

    def test_row_past_the_end_clamps_to_the_last_line(self):
        result, _ = self.insert("a\nb\n", row=99)
        self.assertRegex(result, r"^a\nb\n" + self.HEX + r"$")

    def test_column_past_the_end_clamps_to_the_line_length(self):
        result, _ = self.insert("ab\n", row=1, column=99)
        self.assertRegex(result, r"^ab" + self.HEX + r"\n$")

    def test_a_file_without_a_trailing_newline_keeps_its_last_line(self):
        result, _ = self.insert("hello", row=1, column=6)
        self.assertRegex(result, r"^hello" + self.HEX + r"$")

    def test_print_only_leaves_the_file_alone(self):
        result, output = self.insert("hello\n", row=2, print_only=True)
        self.assertEqual(result, "hello\n")
        self.assertIn("would insert", output)
        self.assertIn("buffer.twig:2:1", output)

    def test_reported_position_matches_where_it_landed(self):
        _, output = self.insert("hello\n", row=2)
        self.assertIn("buffer.twig:2:1", output)

    def test_without_a_file_it_prints_a_uuid(self):
        args = argparse.Namespace(
            target=None, row=1, column=1, print_only=False, root=os.getcwd()
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sw.run_uuid(args, None)
        self.assertRegex(stdout.getvalue().strip(), r"^" + self.HEX + r"$")

    def test_values_do_not_repeat(self):
        seen = {self.insert("\n", row=1)[0].strip() for _ in range(10)}
        self.assertEqual(len(seen), 10)


class Examples(unittest.TestCase):
    """The example config is the product here, so it is checked like code."""

    def setUp(self):
        self.tasks = load_jsonc("examples/tasks.json")
        self.keymap = load_jsonc("examples/keymap.json")
        self.labels = {task["label"] for task in self.tasks}

    def test_every_binding_names_a_task_that_exists(self):
        # Zed does nothing at all when a task_name does not match, with no
        # error anywhere, so a rename can break bindings invisibly.
        for group in self.keymap:
            for key, binding in group["bindings"].items():
                name = binding[1]["task_name"]
                self.assertIn(name, self.labels, f"{key} in {group['context']}")

    def test_labels_are_unique(self):
        self.assertEqual(len(self.labels), len(self.tasks))

    def test_labels_are_verb_first(self):
        verbs = {"go", "insert", "create", "show", "open", "rebuild"}
        for label in self.labels:
            self.assertTrue(label.startswith("Shopware: "), label)
            self.assertIn(label.split(": ", 1)[1].split()[0], verbs, label)

    def test_every_task_names_a_known_action(self):
        known = set(sw.action_names())
        for task in self.tasks:
            action = task["args"][1]
            self.assertIn(action, known, task["label"])

    def test_tasks_touching_a_file_declare_a_save_strategy(self):
        # sw-action.py reads the file from disk, so an unsaved buffer is stale.
        # Whether a task needs the flush depends on the action, which the test
        # cannot infer, so require the choice to be explicit and reviewable.
        for task in self.tasks:
            if "$ZED_FILE" in task["args"]:
                self.assertIn(task.get("save"), ("current", "none"), task["label"])

    def test_no_binding_is_used_twice_in_one_context(self):
        for group in self.keymap:
            keys = list(group["bindings"])
            self.assertEqual(len(keys), len(set(keys)), group["context"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
