"""Tests for scripts/digest.py. Runs with pytest, or directly: python tests/test_digest.py"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import digest  # noqa: E402

# Obviously fake values shaped like real credentials.
FAKE_KEY = "sk-" + "ab12" * 8
FAKE_PAT = "ghp_" + "Z9y8" * 6


def test_redacts_known_secret_shapes():
    text = f"use {FAKE_KEY} and {FAKE_PAT} then email jo.doe@example.com"
    out = digest.redact(text)
    assert FAKE_KEY not in out and FAKE_PAT not in out
    assert "jo.doe@example.com" not in out
    assert out.count("<secret>") == 2 and "<email>" in out


def test_redacts_keyword_values_but_keeps_the_keyword():
    out = digest.redact("password: hunter2hunter2 and api_key=abcdef123456")
    assert "hunter2hunter2" not in out and "abcdef123456" not in out
    assert "password" in out and "api_key" in out


def test_redacts_long_mixed_tokens_but_not_plain_words():
    token = "A1" * 25
    word = "internationalization-of-the-reporting-pipeline-and-its-dashboards"
    assert digest.redact(f"id {token}") == "id <secret>"
    assert digest.redact(word) == word


def test_home_directory_becomes_tilde():
    home = str(Path.home())
    assert digest.redact(f"{home}/notes/a.md").startswith("~")


def test_project_slug_matches_claude_code_naming():
    assert digest.project_slug(r"C:\Users\Sam\My Project") == "C--Users-Sam-My-Project"


def line(kind, ts, content, **extra):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}, **extra})


def build_logs(root: Path, project: Path):
    log_dir = root / "logs" / digest.project_slug(project)
    log_dir.mkdir(parents=True)
    t = "2026-10-15T10:{:02d}:00Z".format
    rows = [
        line("user", t(0), f"Summarise the Q3 notes, my key is {FAKE_KEY}"),
        line("assistant", t(1), [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "run-report"}}]),
        line("user", t(2), [{"type": "tool_result", "tool_use_id": "a", "is_error": True,
                              "content": "command not found: run-report"}]),
        line("assistant", t(3), [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "run-report"}}]),
        line("user", t(4), [{"type": "tool_result", "tool_use_id": "b", "is_error": True,
                              "content": "command not found: run-report"}]),
        line("user", t(5), "[Request interrupted by user]"),
        line("user", t(6), "No, I said use the template in docs/ first"),
        line("assistant", t(7), [{"type": "tool_use", "id": "c", "name": "Write",
                                   "input": {"file_path": str(project / "docs" / "out.md")}}]),
        line("user", t(8), [{"type": "tool_result", "tool_use_id": "c", "is_error": True,
                              "content": "The user doesn't want to proceed with this tool use."}]),
        line("user", t(9), "<command-name>/review</command-name><command-args>again</command-args>"),
    ]
    log = log_dir / "s1.jsonl"
    log.write_text("\n".join(rows), encoding="utf-8")
    old = time.time() - 3600
    os.utime(log, (old, old))
    return root / "logs"


def run_digest(project, logs, *args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "digest.py"), "--project", str(project), "--logs", str(logs),
         "--since", "2026-10-01", *args],
        capture_output=True, text=True, encoding="utf-8", env=env)


def test_digest_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "My Project"
        (project / "docs").mkdir(parents=True)
        logs = build_logs(root, project)
        r = run_digest(project, logs)
        assert r.returncode == 0, r.stderr
        text = (project / ".self-improver" / "digest.md").read_text(encoding="utf-8")
        assert "1 sessions plus 0 scheduled runs" in text
        assert FAKE_KEY not in text
        assert "×2 Bash: command not found: run-report" in text          # repeated error is grouped
        assert "No, I said use the template in docs/ first" in text       # interruption follow-up and correction
        assert "Declined tool calls" in text and "Write" in text
        assert "/review" in text
        assert "docs" in text                                              # project folder touched
        ignore = (project / ".self-improver" / ".gitignore").read_text(encoding="utf-8")
        assert "digest.md" in ignore


def test_mark_records_state_and_removes_digest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "p"
        project.mkdir()
        logs = build_logs(root, project)
        run_digest(project, logs)
        r = subprocess.run([sys.executable, str(SCRIPTS / "digest.py"), "--project", str(project), "--logs", str(logs),
                            "--mark"], capture_output=True, text=True, encoding="utf-8")
        assert "Last review recorded" in r.stdout
        assert (project / ".self-improver" / "state.json").exists()
        assert not (project / ".self-improver" / "digest.md").exists()


def test_missing_logs_give_a_clear_message():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "p"
        project.mkdir()
        r = run_digest(project, Path(tmp) / "nowhere")
        assert "No session logs found" in r.stdout


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("pass ", name)
        except AssertionError as e:
            failed += 1
            print("FAIL ", name, e)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
