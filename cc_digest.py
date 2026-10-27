"""Condense Claude Code transcripts into a small per-project digest for the daily briefs."""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MAX_CHARS = 40_000
PROMPT_CHARS = 300
ANSWER_CHARS = 500
MONDAY = 0
HERE = Path(__file__).resolve().parent


def is_weekly(kind, now):
    """Monday morning is the weekly summary: it covers all of last week."""
    return kind == "am" and now.astimezone(IST).weekday() == MONDAY


def _midnight(day):
    return datetime.combine(day, datetime.min.time(), IST)


def since_for(kind, briefs_dir, now):
    today = now.astimezone(IST).date()
    if is_weekly(kind, now):
        return _midnight(today - timedelta(days=7))
    if kind == "pm":
        return _midnight(today - timedelta(days=1))
    # A catch-up run after a sleep, or a Monday, must cover the whole gap since the last
    # morning brief; 72h stops a week away from flooding one message.
    am_briefs = list(Path(briefs_dir).glob("*-am.md"))
    if not am_briefs:
        return now - timedelta(hours=24)
    last = datetime.fromtimestamp(max(p.stat().st_mtime for p in am_briefs), IST)
    return min(max(last, now - timedelta(hours=72)), now - timedelta(hours=24))


def _records(path):
    with open(path, errors="ignore") as lines:
        for line in lines:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # the newest line of a live session's transcript is often half-written
                continue


def _typed_text(block):
    text = block.strip()
    if text.startswith("<pasted_content"):
        inner = text.split(">", 1)[1].strip()
        return "[pasted] " + inner.splitlines()[0] if inner else None
    # other `<…>` blocks are hook output, slash-command echoes and injected instructions, not typed by a person
    if not text or text.startswith("<") or text == "[Request interrupted by user]":
        return None
    return text


def _human_prompt(record):
    content = (record.get("message") or {}).get("content")
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    # the VS Code extension stores a prompt as text blocks with injected context ahead of what was typed
    blocks = [content] if isinstance(content, str) else [
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    typed = [t for t in map(_typed_text, blocks) if t]
    return " ".join(typed) or None


def _assistant_text(record):
    if record.get("type") != "assistant":
        return None
    texts = [block.get("text", "") for block in (record.get("message") or {}).get("content") or []
             if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(t for t in texts if t.strip()) or None


def _project(cwd):
    return "/".join(Path(cwd or "unknown").parts[-2:])


def _one_line(text, limit):
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _collect(projects_dir, since, until):
    """project -> session -> {"prompts": [(time, text)], "answer": (time, text)}"""
    sessions = defaultdict(lambda: defaultdict(lambda: {"prompts": [], "answer": None}))
    for path in Path(projects_dir).glob("*/*.jsonl"):
        if datetime.fromtimestamp(path.stat().st_mtime, IST) < since:
            continue
        for record in _records(path):
            try:
                _add(sessions, record, since, until)
            except (AttributeError, TypeError, ValueError):
                # one odd record must not cost the brief its whole Claude Code section
                continue
    return sessions


def _add(sessions, record, since, until):
    if record.get("isSidechain") or not record.get("timestamp"):
        return
    at = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    if not since <= at < until:
        return
    prompt = _human_prompt(record)
    answer = _assistant_text(record)
    if not (prompt or answer):
        return
    session = sessions[_project(record.get("cwd"))][record.get("sessionId")]
    if prompt:
        session["prompts"].append((at, prompt))
    if answer and (session["answer"] is None or at >= session["answer"][0]):
        session["answer"] = (at, answer)


def _render_project(name, sessions, budget):
    """Keeps the newest prompts that fit the budget; older ones collapse into a `(+N more)` note."""
    blocks = []
    prompted = [s for s in sessions.values() if s["prompts"]]
    for session in sorted(prompted, key=lambda s: min(s["prompts"])[0]):
        lines = [f"- [{at.astimezone(IST):%H:%M}] {_one_line(text, PROMPT_CHARS)}" for at, text in sorted(session["prompts"])]
        if session["answer"]:
            lines.append(f"  → {_one_line(session['answer'][1], ANSWER_CHARS)}")
        blocks.append(lines)
    if not blocks:
        return ""
    header = f"## {name}"
    all_lines = [line for block in blocks for line in block]
    kept, used = [], len(header) + 20
    for line in reversed(all_lines):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(all_lines) - len(kept)
    body = ([f"(+{dropped} more)"] if dropped else []) + list(reversed(kept))
    return "\n".join([header, *body])


def build_digest(projects_dir, since, until):
    sessions = _collect(projects_dir, since, until)
    active = [name for name, s in sessions.items() if any(v["prompts"] for v in s.values())]
    if not active:
        return f"No Claude Code activity between {since:%d %b %H:%M} and {until:%d %b %H:%M}."
    budget = (MAX_CHARS - 2 * len(active)) // len(active)
    sections = [_render_project(name, sessions[name], budget) for name in sorted(active)]
    return "\n\n".join(s for s in sections if s)[:MAX_CHARS]


def window_header(kind, since, until):
    """The first line of the digest: the brief's prompt reuses this window for every other source."""
    line = f"Window: {since.astimezone(IST):%d %b %H:%M} → {until.astimezone(IST):%d %b %H:%M} IST"
    if kind == "am":
        line += f" (late run: {'yes' if until.astimezone(IST).hour >= 12 else 'no'})"
    if is_weekly(kind, until):
        line += " (weekly)"
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["am", "pm"], required=True)
    parser.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--briefs", type=Path, default=HERE / "briefs")
    args = parser.parse_args()
    now = datetime.now(IST)
    since = since_for(args.kind, args.briefs, now)
    print(window_header(args.kind, since, now))
    print()
    print(build_digest(args.projects, since, now))


if __name__ == "__main__":
    main()
