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


def test_redacts_ids_phones_and_long_numbers_but_keeps_dates_and_figures():
    out = digest.redact("session 3d361928-3001-4c85-bb81-442a7221d170, hash a1b2c3d4e5f6a7b8c9d0, "
                        "account 12345678, call +1 250 555 0100 or (250) 555-0100")
    assert "3d361928" not in out and "a1b2c3d4" not in out and "12345678" not in out and "555" not in out
    assert out.count("<id>") == 2 and out.count("<phone>") == 2 and "<number>" in out
    kept = "on 2026-11-02 at 09:58, revenue 1,250,000 and 12.5M, ratio 3.14159265, ticker RKLB"
    assert digest.redact(kept) == kept


def test_listed_names_become_consistent_placeholders():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "names.txt"
        f.write_text("# comment\nJane Doe | Jane\nSam Lee\n", encoding="utf-8")
        digest.NAMES[:] = digest.load_names(f)
        try:
            out = digest.redact("Jane Doe said Jane would ask Sam Lee; Janet is someone else")
        finally:
            digest.NAMES[:] = []
    assert out == "Person A said Person A would ask Person B; Janet is someone else"


def test_corrections_need_a_clear_phrase_or_an_opening_word():
    corrections = ["no, that's the wrong file", "you forgot the chart", "actually make it blue",
                   "why did you delete that", "it still doesn't render", "hmm, don't use that font"]
    ordinary = ["do the same again for the next file", "also, don't forget the sources",
                "can you check whether anything is still missing", "great, now build the deck"]
    assert all(digest.reads_as_correction(m) for m in corrections)
    assert not any(digest.reads_as_correction(m) for m in ordinary)


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


def build_many(root: Path, project: Path, n):
    log_dir = root / "logs" / digest.project_slug(project)
    log_dir.mkdir(parents=True)
    old = time.time() - 3600
    for k in range(n):
        rows = [line("user", "2026-10-15T%02d:00:00Z" % (k % 24), f"Session {k} request " + "x" * 500),
                line("user", "2026-10-15T%02d:01:00Z" % (k % 24), "and a follow-up " + "y" * 250)]
        log = log_dir / f"s{k:02d}.jsonl"
        log.write_text("\n".join(rows), encoding="utf-8")
        os.utime(log, (old + k, old + k))
    return root / "logs"


def test_first_run_writes_parts_and_names_what_was_cut():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "p"
        project.mkdir()
        logs = build_many(root, project, 30)
        work = project / ".self-improver"
        r = run_digest(project, logs, "--max-chars", "8000", "--parts", "2")
        assert r.returncode == 0, r.stderr
        first = (work / "digest.md").read_text(encoding="utf-8")
        assert (work / "digest-2.md").exists() and not (work / "digest-3.md").exists()
        assert first.startswith("> This digest is in 2 parts")
        assert "Cut for length: 18 of 30 sessions" in first and "and 3 more" in first
        assert "Session 12 request" in first                                   # the first session cut is named
        assert "names.txt" in (work / ".gitignore").read_text(encoding="utf-8")
        run_digest(project, logs, "--max-chars", "200000")                     # one file: the old part goes
        assert not (work / "digest-2.md").exists()


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
        assert not list((project / ".self-improver").glob("digest-*.md"))


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
