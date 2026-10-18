"""Condense recent AI coding sessions into one digest for the self-improver-loop skill.

Reads Claude Code session logs (~/.claude/projects/<project>/*.jsonl; main sessions
only, not subagents) and writes a markdown digest of what shows where the assistant
went wrong or where work repeats: your own messages, interruptions and what you said
next, tool calls you declined, repeated tool errors, messages that read as corrections,
slash commands and skills used, and which project folders each session touched.
Scheduled-task runs are counted separately, since their first message is a stored
prompt, not a request.

Read-only on the logs. Standard library only. Secrets, emails and home-directory paths
are redacted before anything is written.

  python digest.py                     # this project, since the last --mark (7 days the first time)
  python digest.py --days 30           # a fixed window
  python digest.py --since 2026-09-01  # from a date
  python digest.py --all-projects      # every project in the log folder
  python digest.py --mark              # record now as the last review
  python digest.py --project PATH      # a project other than the current folder

Other agents: only the log reader is specific to Claude Code. Point --logs at a folder
of Claude Code style .jsonl files, or adapt read_session() to your agent's log format.
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

MAX_CHARS = 60_000     # fits in one read
SIGNAL_SHARE = 0.45    # share of the budget for the summary sections
FIRST_CHARS = 600
FOLLOW_CHARS = 300
FOLLOW_MAX = 10
SKIP_RECENT_S = 120    # a log written in the last two minutes is the session running now

CORRECTION = re.compile(
    r"(^|[.!?]\s+)no\b|\bnope\b|\bwrong\b|\bincorrect\b|\bnot what i\b|\bthat'?s not\b|\bi said\b|"
    r"\bi told you\b|\bi asked\b|\bi already\b|\byou forgot\b|\byou missed\b|\byou didn'?t\b|"
    r"\bwhy did you\b|\bwhy are you\b|\bwhy is\b|\bstop\b|\bdon'?t\b|\bdo not\b|\bundo\b|\brevert\b|"
    r"\bredo\b|\bagain\b|\bstill\b|\bdoesn'?t work\b|\bnot working\b|\bbroken\b|\binstead\b|"
    r"\bactually\b|\bshould have\b|\bshouldn'?t\b|\bnever\b|\bwhere is\b|\bmissing\b",
    re.I)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Things that must never reach the digest. Specific shapes first, generic ones last.
SECRETS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
]
KEYWORD_VALUE = re.compile(
    r"(?i)\b(bearer|token|api[_ -]?key|secret|password|passwd|pwd|authorization)\b"
    r"(\s*[:=]\s*|\s+)([\"']?)[^\s\"']{6,}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def redact(s):
    for rx in SECRETS:
        s = rx.sub("<secret>", s)
    s = KEYWORD_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<secret>", s)
    s = EMAIL.sub("<email>", s)
    s = LONG_TOKEN.sub(
        lambda m: "<secret>" if re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0)) else m.group(0), s)
    home = str(Path.home())
    for h in {home, home.replace("\\", "/")}:
        s = re.sub(re.escape(h), lambda m: "~", s, flags=re.I)
    return s


def text_blocks(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


def tidy(raw):
    """Turn a raw user message into the words that were typed."""
    s = re.sub(r"<(system-reminder|local-command-stdout|local-command-caveat|ide_[a-z_]+)>.*?</\1>", "", raw, flags=re.S)
    s = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content>",
               lambda m: f"[pasted text, {len(m.group(1))} chars]", s, flags=re.S)
    cmd = re.search(r"<command-name>/?(.*?)</command-name>", s)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
        s = f"/{cmd.group(1).strip()} {args.group(1).strip() if args else ''}"
    s = re.sub(r'@"([^"]+)"', lambda m: "@" + Path(m.group(1)).name, s)
    s = re.sub(r"</?[a-zA-Z_-]{1,40}[^>]{0,200}>", "", s)
    return redact(re.sub(r"\s+", " ", s).strip())


def norm_error(s):
    s = UUID.sub("<id>", redact(s))
    s = re.sub(r"[A-Za-z]:[\\/][^\s'\"]+", "<path>", s)
    s = re.sub(r"\d+", "N", s)
    return re.sub(r"\s+", " ", s).strip()[:150]


def project_slug(path):
    """Claude Code names a project's log folder after its path, every non-alphanumeric character as a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def find_log_dirs(logs, project, every):
    if not logs.is_dir():
        return []
    if every:
        return [d for d in sorted(logs.glob("*")) if d.is_dir()]
    exact = logs / project_slug(project)
    if exact.is_dir():
        return [exact]
    tail = project_slug(project.name)
    near = [d for d in logs.glob("*") if d.is_dir() and d.name.endswith("-" + tail)]
    return near if len(near) == 1 else []


def make_area(root):
    prefix = str(root).replace("\\", "/").lower().rstrip("/") + "/"

    def area(path):
        p = str(path).replace("\\", "/")
        if not p.lower().startswith(prefix):
            return None
        rest = p[len(prefix):]
        head = rest.split("/", 1)[0]
        if head.startswith(".") and rest.count("/") >= 2:
            head = head + "/" + rest.split("/")[1]
        return head if "/" in rest else "(root)"
    return area


def target_of(inp):
    for k in ("file_path", "notebook_path", "path", "url", "skill"):
        if isinstance(inp.get(k), str):
            return inp[k]
    for k in ("command", "query", "pattern"):
        if isinstance(inp.get(k), str):
            return inp[k][:100]
    return ""


def when(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return None


def read_session(path, since, area_of):
    s = {"file": path, "title": None, "ai_title": None, "start": None, "end": None,
         "humans": [], "interrupts": [], "declined": [], "errors": [], "tools": Counter(),
         "skills": Counter(), "commands": Counter(), "areas": Counter(), "task": None}
    uses = {}
    after_interrupt = False
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "custom-title":
            s["title"] = d.get("customTitle") or s["title"]
        elif t == "agent-name" and not s["title"]:
            s["title"] = d.get("agentName")
        elif t == "ai-title":
            s["ai_title"] = d.get("aiTitle")
        if d.get("isSidechain"):
            continue
        ts = d.get("timestamp")
        w = when(ts)
        old = w is not None and w.timestamp() < since    # resumed sessions carry their older history
        if ts and not old:
            s["start"] = s["start"] or ts
            s["end"] = ts
        content = (d.get("message") or {}).get("content")

        if t == "assistant" and isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name, inp = b.get("name", "?"), b.get("input") or {}
                short = re.sub(r"^mcp__[0-9a-f-]{20,}__", "mcp__", name)
                uses[b.get("id")] = (short, target_of(inp))
                s["tools"][short] += 1
                if name == "Skill" and inp.get("skill"):
                    s["skills"][inp["skill"]] += 1
                for k in ("file_path", "notebook_path", "path"):
                    area = area_of(inp.get(k, "")) if isinstance(inp.get(k), str) else None
                    if area:
                        s["areas"][area] += 1
            continue
        if t != "user" or old:
            continue

        if isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")):
                    continue
                c = b.get("content")
                msg = c if isinstance(c, str) else text_blocks(c)
                tool, target = uses.get(b.get("tool_use_id"), ("?", ""))
                if "doesn't want to proceed" in msg or d.get("toolUseResult") == "User rejected tool use":
                    s["declined"].append(redact(f"{tool} {target}".strip()))
                else:
                    s["errors"].append((tool, norm_error(re.sub(r"<[^>]+>", "", msg))))
        if d.get("toolUseResult") is not None or d.get("isMeta"):
            continue
        raw = text_blocks(content)
        if not raw.strip():
            continue
        if raw.startswith("[Request interrupted by user"):
            s["interrupts"].append(None)
            after_interrupt = True
            continue
        if "<task-notification>" in raw or "<ci-monitor-event" in raw:
            continue
        if not s["humans"]:
            m = re.match(r'\s*<scheduled-task name="([^"]+)"', raw)
            if m:
                s["task"] = m.group(1)
        cmd = re.search(r"<command-name>/?(.*?)</command-name>", raw)
        if cmd:
            s["commands"][cmd.group(1).strip()] += 1
        text = tidy(raw)
        if not text:
            continue
        if after_interrupt and s["interrupts"]:
            s["interrupts"][-1] = text
            after_interrupt = False
        s["humans"].append(text)
    return s


def minutes(s):
    a, b = when(s["start"]), when(s["end"])
    return round((b - a).total_seconds() / 60) if a and b else 0


def installed_skills(dirs):
    names = set()
    for d in dirs:
        if d.is_dir():
            names.update(p.parent.name for p in d.glob("*/SKILL.md"))
    return names


def clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float)
    ap.add_argument("--since")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--project", type=Path, default=Path.cwd(), help="project folder (default: current folder)")
    ap.add_argument("--all-projects", action="store_true", help="read every project in the log folder")
    ap.add_argument("--logs", type=Path, default=Path.home() / ".claude" / "projects",
                    help="folder holding one sub-folder of .jsonl logs per project")
    ap.add_argument("--workspace", type=Path,
                    help="where state, digest and reports live (default: <project>/.self-improver)")
    a = ap.parse_args()

    project = a.project.resolve()
    work = (a.workspace or project / ".self-improver").resolve()
    state_file, digest_file = work / "state.json", work / "digest.md"

    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    if a.mark:
        work.mkdir(parents=True, exist_ok=True)
        state["last_review"] = time.time()
        state_file.write_text(json.dumps(state), encoding="utf-8")
        digest_file.unlink(missing_ok=True)
        print("Last review recorded:", time.strftime("%Y-%m-%d %H:%M"))
        return
    now = time.time()
    if a.since:
        since = datetime.fromisoformat(a.since).timestamp()
    elif a.days:
        since = now - a.days * 86400
    else:
        since = state.get("last_review", now - 7 * 86400)

    log_dirs = find_log_dirs(a.logs, project, a.all_projects)
    if not log_dirs:
        print(f"No session logs found for {project} under {a.logs}.\n"
              "Run this from the project folder, pass --project, or use --all-projects.")
        return
    files = sorted((p for d in log_dirs for p in d.glob("*.jsonl")
                    if since < p.stat().st_mtime < now - SKIP_RECENT_S), key=lambda p: p.stat().st_mtime)
    area_of = make_area(project)
    sessions, seen = [], {}
    for s in (read_session(p, since, area_of) for p in files):
        if not s["humans"]:
            continue
        key = tuple(s["humans"][:4])        # a resumed or forked session repeats its opening; keep the latest copy
        if key in seen:
            sessions[seen[key]] = s
        else:
            seen[key] = len(sessions)
            sessions.append(s)
    live = [s for s in sessions if not s["task"]]
    tasks = [s for s in sessions if s["task"]]

    tools, skills, cmds, areas, errs = Counter(), Counter(), Counter(), Counter(), Counter()
    for s in sessions:
        tools.update(s["tools"]); skills.update(s["skills"]); cmds.update(s["commands"])
        areas.update({k: 1 for k in s["areas"]})
        errs.update(f"{tool}: {e}" for tool, e in s["errors"])

    def label(s):
        return clip(redact(s["title"] or s["ai_title"] or s["humans"][0]), 70)

    def stamp(s):
        w = when(s["start"])
        return w.strftime("%Y-%m-%d %H:%M") if w else "?"

    scope = "all projects" if a.all_projects else project.name
    out = [f"# Session digest ({scope}): {len(live)} sessions plus {len(tasks)} scheduled runs",
           f"Window: {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} to "
           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
           "", "## Totals", "",
           "| Measure | Value |", "|---|---|",
           f"| Your messages | {sum(len(s['humans']) for s in live)} |",
           f"| Interruptions | {sum(len(s['interrupts']) for s in sessions)} |",
           f"| Declined tool calls | {sum(len(s['declined']) for s in sessions)} |",
           f"| Tool errors | {sum(len(s['errors']) for s in sessions)} |",
           f"| Project folders touched (sessions) | {', '.join(f'{k} {n}' for k, n in areas.most_common(15)) or 'none'} |",
           f"| Skills run | {', '.join(f'{k} ×{n}' for k, n in skills.most_common()) or 'none'} |",
           f"| Slash commands | {', '.join(f'/{k} ×{n}' for k, n in cmds.most_common()) or 'none'} |",
           f"| Top tools | {', '.join(f'{k} {n}' for k, n in tools.most_common(15)) or 'none'} |"]
    unused = sorted(installed_skills([project / ".claude" / "skills", Path.home() / ".claude" / "skills"])
                    - set(skills) - set(cmds))
    out += ["", f"Installed skills not run in this window: {', '.join(unused) or 'none'}"]

    if tasks:
        out += ["", "## Scheduled runs", "", "| Task | Runs | Runs with tool errors | Tool errors |", "|---|---|---|---|"]
        for name in sorted({s["task"] for s in tasks}):
            runs = [s for s in tasks if s["task"] == name]
            out.append(f"| {name} | {len(runs)} | {sum(1 for s in runs if s['errors'])} | "
                       f"{sum(len(s['errors']) for s in runs)} |")

    out += ["", "## Interruptions and what you said next"]
    rows = [(s, t) for s in sessions for t in s["interrupts"]]
    out += [f"- {stamp(s)} · {label(s)} → {clip(t, FOLLOW_CHARS) if t else '(nothing after it)'}"
            for s, t in rows] or ["- none"]

    out += ["", "## Declined tool calls"]
    out += [f"- {stamp(s)} · {label(s)} → {clip(x, 160)}" for s in sessions for x in s["declined"]] or ["- none"]

    out += ["", "## Tool errors seen more than once"]
    out += [f"- ×{n} {e}" for e, n in errs.most_common(25) if n > 1] or ["- none"]

    out += ["", "## Follow-ups that read as corrections"]
    corr = [(s, h) for s in live for h in s["humans"][1:] if CORRECTION.search(h[:250])]
    out += [f"- {stamp(s)} · {label(s)} → {clip(h, FOLLOW_CHARS)}" for s, h in corr[:60]] or ["- none"]
    if len(corr) > 60:
        out.append(f"- … {len(corr) - 60} more")

    head = "\n".join(out)
    head = head[: int(MAX_CHARS * SIGNAL_SHARE)]

    body = ["", "## Sessions (first message is the request; then your follow-ups)"]
    for s in live:
        areas_s = ", ".join(k for k, _ in s["areas"].most_common(4)) or "no project files"
        body.append(f"\n### {stamp(s)} · {label(s)} · {len(s['humans'])} msgs · {minutes(s)} min · "
                    f"{len(s['errors'])} errors · {areas_s}")
        for i, h in enumerate(s["humans"][: FOLLOW_MAX + 1]):
            body.append(f"{'>' if i == 0 else '-'} {clip(h, FIRST_CHARS if i == 0 else FOLLOW_CHARS)}")
        if len(s["humans"]) > FOLLOW_MAX + 1:
            body.append(f"- … {len(s['humans']) - FOLLOW_MAX - 1} more messages")
    text = head + "\n".join(body)[: MAX_CHARS - len(head)]

    work.mkdir(parents=True, exist_ok=True)
    (work / ".gitignore").write_text("digest.md\nbackups/\n", encoding="utf-8")   # the digest holds your messages
    digest_file.write_text(text, encoding="utf-8")
    print(f"{len(live)} sessions and {len(tasks)} scheduled runs -> {digest_file} ({len(text):,} chars)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
