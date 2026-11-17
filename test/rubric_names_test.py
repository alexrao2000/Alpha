"""Rubric discovery supports short names, IDs, aliases, and explicit paths."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from alpha.cli import expand_quick_command, main, parser
from alpha.rubrics import available_rubrics, load_rubric, resolve_rubric


class NamedRubricTests(unittest.TestCase):
    def test_default_and_bundled_writing_shorthand(self):
        self.assertEqual(load_rubric("default")["id"], "general-output-quality")
        self.assertEqual(load_rubric("general")["id"], "general-output-quality")
        self.assertEqual(load_rubric("writing")["id"], "writing-quality")
        self.assertEqual(load_rubric("writing-quality")["id"], "writing-quality")

    def test_cli_accepts_r_and_legacy_long_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "experiment.json"
            path.write_text("{}", encoding="utf-8")
            for spelling in ("-r", "--rubric"):
                with self.subTest(spelling=spelling):
                    args = expand_quick_command(
                        parser().parse_args(["q", "judge", str(path), spelling, "writing"])
                    )
                    self.assertEqual(args.rubric, "writing")
                    self.assertEqual(load_rubric(args.rubric)["id"], "writing-quality")
            args = parser().parse_args(
                ["qualitative", str(path), "--llm-judge", "-r", "writing"]
            )
            self.assertEqual(args.rubric, "writing")

    def test_custom_name_and_declared_id(self):
        with tempfile.TemporaryDirectory() as d:
            prior = Path.cwd()
            try:
                os.chdir(d)
                folder = Path("rubrics")
                folder.mkdir()
                custom = deepcopy(load_rubric())
                custom["id"] = "review-v2"
                path = folder / "my-review.json"
                path.write_text(json.dumps(custom), encoding="utf-8")
                self.assertEqual(load_rubric("my-review")["id"], "review-v2")
                self.assertEqual(load_rubric("review-v2")["id"], "review-v2")
                self.assertEqual(load_rubric(path)["id"], "review-v2")
                self.assertEqual(load_rubric(str(path))["id"], "review-v2")
                listed = [e for e in available_rubrics() if e["name"] == "my-review"]
                self.assertEqual(len(listed), 1)
            finally:
                os.chdir(prior)

    def test_ambiguity_requires_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            prior = Path.cwd()
            try:
                os.chdir(d)
                folder = Path("rubrics")
                folder.mkdir()
                custom = deepcopy(load_rubric())
                custom["id"] = "writing-quality"
                path = folder / "something.json"
                path.write_text(json.dumps(custom), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Ambiguous"):
                    resolve_rubric("writing")
                self.assertEqual(load_rubric(path)["id"], "writing-quality")
            finally:
                os.chdir(prior)

    def test_unknown_name_and_bad_path_do_not_fallback(self):
        with self.assertRaisesRegex(ValueError, "Unknown rubric"):
            load_rubric("absent-rubric-928374")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_rubric("rubrics/nope.json")

    def test_invalid_named_json_skipped_but_explicit_path_fails(self):
        with tempfile.TemporaryDirectory() as d:
            prior = Path.cwd()
            try:
                os.chdir(d)
                Path("rubrics").mkdir()
                broken = Path("rubrics/broken.json")
                broken.write_text('{"id": "broken"}', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Unknown rubric"):
                    load_rubric("broken")
                with self.assertRaisesRegex(ValueError, "keys"):
                    load_rubric(broken)
            finally:
                os.chdir(prior)

    def test_cli_lists_short_names(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["rubrics"]), 0)
        self.assertIn("default", output.getvalue())
        self.assertIn("writing", output.getvalue())


if __name__ == "__main__":
    unittest.main()
