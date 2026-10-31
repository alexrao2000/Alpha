# Generated programs run in a separate macOS sandbox with no network or access to
# home-directory data. Only test inputs enter that process; expected answers stay
# in this grader. Resource limits bound execution time and output size.

import ast
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile


def extract_code(text):
    import re
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    code = max(blocks, key=len) if blocks else text.strip()
    ast.parse(code)
    return code


def evaluate(code, checks, timeout=8):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError("The generated-code grader currently requires macOS sandbox-exec.")
    code = extract_code(code)
    with tempfile.TemporaryDirectory(prefix="alpha-grade-") as temporary:
        directory = Path(temporary).resolve()
        artifact = directory / "solution.py"
        artifact.write_text(code)
        runner = directory / "run.py"
        runner.write_text(
            "import json,runpy,sys\n"
            "requests=json.load(sys.stdin)\n"
            "module=runpy.run_path(sys.argv[1],run_name='alpha_solution')\n"
            "results=[]\n"
            "for request in requests:\n"
            " try: results.append({'value':module['solve'](request)})\n"
            " except Exception as e: results.append({'error':type(e).__name__})\n"
            "print(json.dumps(results))\n"
        )
        readable = ["/System", "/usr", "/opt/homebrew", str(Path(sys.base_prefix).resolve()), str(directory)]
        read_filters = ' '.join(f'(subpath {json.dumps(p)})' for p in readable)
        read_filters += ' (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random")'
        profile = '(version 1)(allow default)(deny network*)(deny process-fork)'
        profile += f'(deny file-read-data (require-not (require-any {read_filters})))'
        profile += f'(deny file-write* (require-not (require-any (subpath {json.dumps(str(directory))}) (literal "/dev/null"))))'
        policy = directory / "policy.sb"
        policy.write_text(profile)
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
            resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        with (directory / "stdout").open("w") as output, (directory / "stderr").open("w") as errors:
            completed = subprocess.run(
                ["/usr/bin/sandbox-exec", "-f", str(policy), str(Path(sys.executable).resolve()), "-I", "-B", str(runner), str(artifact)],
                input=json.dumps([c["request"] for c in checks]), text=True, stdout=output, stderr=errors,
                timeout=timeout, cwd=directory, env={"PATH": "/usr/bin:/bin", "HOME": str(directory)}, preexec_fn=limits,
            )
        if completed.returncode:
            return {"passed": False, "unsafe": False, "checks": [{"name": "Program execution", "passed": False}],
                    "execution_error": f"Generated program exited {completed.returncode}: " + (directory / "stderr").read_text()[:600], "results": []}
        try:
            values = json.loads((directory / "stdout").read_text())
        except ValueError:
            values = []
        outcomes = [{"name": c["name"], "passed": i < len(values) and
                     json.dumps(values[i], sort_keys=True) == json.dumps({"value": c["expected"]}, sort_keys=True)}
                    for i, c in enumerate(checks)]
        return {"passed": len(values) == len(checks) and all(c["passed"] for c in outcomes),
                "unsafe": False, "checks": outcomes, "results": values}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        result = evaluate(request["code"], request["checks"])
    except Exception as error:
        result = {"passed": False, "unsafe": False, "checks": [{"name": "Program execution", "passed": False}],
                  "execution_error": f"{type(error).__name__}: {error}", "results": []}
    print(json.dumps(result))
