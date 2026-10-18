# Self improver loop

An AI skill that reads your recent coding-assistant sessions, finds what keeps going wrong, and hands you the exact edits that would stop it. Each review also checks whether the last round of edits worked.

It works with any capable AI model. The skill is a plain Markdown prompt (`SKILL.md`) in the open Agent Skills format plus one Python script. The script reads Claude Code's session logs out of the box; for another agent you point it at logs in that format or adapt one function.

## What it does

Most people fix an assistant's mistakes one at a time, in chat, and then fix the same mistake again next week. This skill looks across sessions for the repeats:

| It looks for | Example |
|---|---|
| Corrections you keep making | You said "use the template first" in four sessions, and the rule is already in your instructions file |
| Tool errors the assistant keeps hitting | The same "command not found" in six sessions |
| Work you do by hand every time | The same five-step request typed again each Monday |
| Actions you decline | Edits to a folder you never want touched |
| Stale setup | Skills you haven't run in months, rules that contradict each other |

It then writes a short report. Every recommendation carries a date, the session title and a quote of what you said, so you can see why it was made. Every recommendation also has the exact text to paste, or the exact edit, ready to apply. Nothing changes until you reply with the numbers you want.

## The loop

| Step | What happens |
|---|---|
| 1. Digest | `scripts/digest.py` condenses the logs for this project into one file. |
| 2. Read | The assistant reads your instructions file, memory, skills and earlier reports. |
| 3. Check | For every change applied last time, it looks for the same problem afterwards: `worked`, `not working` or `too early`. |
| 4. Find | It looks for new patterns. A finding needs at least two occurrences. |
| 5. Report | Up to 10 numbered recommendations, saved to `.self-improver/reports/`. |
| 6. Apply | You pick numbers. It backs up the files, makes exactly those edits, and marks each one `done`, `rejected` or `later`. |

Step 3 is what separates this from a one-off audit. A rule that was added and still gets broken is not added a second time. It gets reworded, moved higher in the file, or turned into a hook or script.

## Privacy

The digest contains snippets of your own messages, so the script treats it as sensitive:

- It runs only on your machine, reads the logs and never sends anything anywhere.
- It redacts API keys, tokens, `password=` style values, private keys, emails and your home directory before writing.
- It writes to `.self-improver/` inside your project with a `.gitignore` that excludes the digest and backups.
- `--mark` deletes the digest at the end of each review.

Redaction is pattern-based, so it can miss a secret in an unusual format. Skim the digest before sharing any of it.

## Requirements

| Need | Why |
|---|---|
| Python 3.9 or newer | Runs `scripts/digest.py`. Standard library only, nothing to install. |
| Claude Code session logs in `~/.claude/projects/` | What the script reads. Other agents: see below. |
| An agent that can run commands and edit files | To run the loop and apply the edits you pick. |

## Install

| Where you run it | How |
|---|---|
| An agent that reads skills folders (Claude Code, OpenAI Codex, Gemini CLI, GitHub Copilot, Cursor and the others listed at [agentskills.io](https://agentskills.io)) | Clone this repo into the agent's skills folder as `self-improver-loop`, for example `git clone https://github.com/Vidwaansinghania/Self-Improver-Loop.git ~/.claude/skills/self-improver-loop`. Each agent documents its own folder. |
| An agent with a different log format | Same install, then adapt `read_session()` in `scripts/digest.py` to read your agent's logs, or convert them to Claude Code's JSONL shape and pass the folder with `--logs`. |

## Using it

From the project you want reviewed:

```
Run the self improver loop on this project.
```

Running the digest on its own:

```bash
python scripts/digest.py                      # since the last review, or 7 days the first time
python scripts/digest.py --days 30            # a fixed window
python scripts/digest.py --since 2026-09-01   # from a date
python scripts/digest.py --all-projects       # every project in the log folder
python scripts/digest.py --mark               # record this review and delete the digest
```

On Windows the command is usually `python` or `py` in place of `python3`.

Then reply with what you want applied: `apply 1, 3, 5`, `do 2`, `all`, or `park 4`.

## Tests

```bash
python tests/test_digest.py
```

Eight tests, using invented logs and obviously fake keys: redaction of secrets, emails and home paths, project folder naming, the digest sections end to end, the `--mark` step, and the message shown when no logs exist.

## Limits

- It sees what you typed and what the tools returned, not what you were thinking. A correction that never made it into chat doesn't appear.
- The correction filter matches common words ("no", "again", "I said"), so many hits are ordinary follow-ups. The assistant reads each and keeps only the real ones.
- A pattern needs two occurrences. A new problem that happened once won't be reported yet.
- Sessions older than your log retention are gone.

## Licence

Apache-2.0. See [LICENSE](LICENSE). Anyone who uses or builds on this work must keep the [NOTICE](NOTICE) file and credit Vidwaan Singhania.
