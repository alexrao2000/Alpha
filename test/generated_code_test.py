# These checks exercise actual sandboxed execution and oracle separation. They
# require macOS; generated programs receive inputs but never expected answers.

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("alpha_grade", ROOT / "integrations/crewai-game-builder/grade.py")
grader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grader)


@unittest.skipUnless(sys.platform == "darwin", "Requires the macOS sandbox grader")
class GeneratedCodeTests(unittest.TestCase):
    def check(self, code, expected, request=None):
        return grader.evaluate(code, [{"name": "contract", "request": request or {}, "expected": expected}])

    def test_functional_result_and_fenced_code(self):
        result = self.check('```python\ndef solve(r): return r["number"] + 2\n```', 7, {"number": 5})
        self.assertTrue(result["passed"])

    def test_wrong_output_and_wrong_json_type_fail(self):
        self.assertFalse(self.check('def solve(r): return 8', 7)["passed"])
        self.assertFalse(self.check('def solve(r): return 1', True)["passed"])

    def test_private_file_is_not_readable(self):
        with tempfile.NamedTemporaryFile(dir=ROOT / "work", mode="w") as secret:
            secret.write("test-only private value"); secret.flush()
            code = f'def solve(r):\n try: return open({secret.name!r}).read()\n except PermissionError: return "denied"'
            self.assertTrue(self.check(code, "denied")["passed"])

    def test_network_is_blocked(self):
        code = 'import socket\ndef solve(r):\n try: socket.socket().connect(("127.0.0.1",9)); return "allowed"\n except PermissionError: return "denied"'
        self.assertTrue(self.check(code, "denied")["passed"])

    def test_expected_answer_is_not_in_program_input(self):
        self.assertFalse(self.check('def solve(r): return r.get("expected", "missing")', "private answer")["passed"])

    def test_mutation_outside_temporary_directory_is_blocked(self):
        path = ROOT / "work" / "forbidden-generated-file"
        code = f'def solve(r):\n try: open({str(path)!r},"w").write("bad"); return "allowed"\n except PermissionError: return "denied"'
        self.assertTrue(self.check(code, "denied")["passed"])
        self.assertFalse(path.exists())

    def test_import_error_is_an_execution_failure(self):
        result = self.check('import definitely_missing_alpha_module\ndef solve(r): return 1', 1)
        self.assertFalse(result["passed"])
        self.assertIn("execution_error", result)


if __name__ == "__main__":
    unittest.main()
