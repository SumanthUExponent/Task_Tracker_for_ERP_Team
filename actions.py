"""Turn your replies to the briefs ("done 1 3", "drop 2", "2 fri", "add …") into a log the next brief applies.

`poll` runs every few minutes from launchd and confirms each reply in Slack; `summary` is what a brief run reads.
No model is involved: replies are parsed by a fixed grammar, and anything else gets a help reply.
"""
import argparse
import json
import re
import shutil
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

import slack_post
from cc_digest import IST, since_for

HERE = Path(__file__).resolve().parent
LOOKBACK = timedelta(days=3)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DONE_WORDS = {"done", "did", "finished", "complete", "completed", "✅"}
DROP_WORDS = {"drop", "dropped", "skip", "cancel", "remove", "❌", "🗑"}
LATER_WORDS = {"later", "snooze", "defer", "postpone", "move", "→", "->"}
POLL_EVERY = timedelta(minutes=55)
CONNECTOR_READ_TOOLS = [
    *(f"mcp__claude_ai_Slack__{t}" for t in ("slack_search_public_and_private", "slack_search_public",
      "slack_read_thread", "slack_read_channel", "slack_read_user_profile", "slack_search_users",
      "slack_search_channels", "slack_get_reactions", "slack_list_user_channels")),
    *(f"mcp__claude_ai_Gmail__{t}" for t in ("search_threads", "get_thread", "get_message", "list_labels")),
    *(f"mcp__claude_ai_Granola_MCP__{t}" for t in ("list_meetings", "get_meetings", "get_meeting_transcript",
      "search_meetings", "query_granola_meetings", "get_account_info")),
    *(f"mcp__claude_ai_Google_Calendar__{t}" for t in ("list_events", "get_event", "search_events", "list_calendars")),
]
# the briefs write to TickTick, so unlike the read-only tools above these include writes; deleting
# is limited to tasks (old summary notes), never lists, tags or habits
TICKTICK_TOOLS = [f"mcp__claude_ai_TickTick__{t}" for t in (
    "list_projects", "create_project", "get_project_by_id", "get_project_with_undone_tasks", "search",
    "search_task", "filter_tasks", "get_task_by_id", "get_task_in_project", "create_task", "batch_add_tasks",
    "update_task", "batch_update_tasks", "complete_task", "move_task", "delete_task", "list_tags", "create_tag",
    "list_completed_tasks_by_date", "list_undone_tasks_by_date")]
HELP = "❓ Didn't catch that. Reply with: done 1 3 · drop 2 · 2 fri · add <task>"


def _next_weekday(today, weekday):
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def _take_date(tokens, today):
    """Removes the date words from tokens and returns the date they name, or None."""
    text = " ".join(tokens)
    if "next week" in text:
        tokens[:] = " ".join(tokens).replace("next week", "").split()
        return _next_weekday(today, 0)
    for i, token in enumerate(tokens):
        found = None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            found = date.fromisoformat(token)
        elif token in ("tomorrow", "tmrw", "tmr"):
            found = today + timedelta(days=1)
        elif len(token) >= 3 and token.isalpha():
            found = next((_next_weekday(today, n) for n, name in enumerate(WEEKDAYS) if name.startswith(token)), None)
        if found:
            del tokens[i]
            return found
    return None


def _parse_clause(clause, today):
    added = re.fullmatch(r"(?:add|todo)\s+(.+)", clause.strip(), re.IGNORECASE)
    if added:
        return [{"action": "add", "text": added.group(1).strip()}]
    tokens = clause.lower().replace(",", " ").split()
    until = _take_date(tokens, today)
    numbers = [int(t) for t in tokens if t.isdigit()]
    words = set(tokens)
    if not numbers:
        return None
    if words & DONE_WORDS:
        return [{"action": "done", "n": n} for n in numbers]
    if words & DROP_WORDS:
        return [{"action": "drop", "n": n} for n in numbers]
    if until or words & LATER_WORDS:
        until = (until or today + timedelta(days=1)).isoformat()
        return [{"action": "later", "n": n, "until": until} for n in numbers]
    return None


def parse(text, today):
    """All commands in text, or [] if any line is not a command (the user then gets the help reply)."""
    actions = []
    for clause in filter(str.strip, re.split(r"[\n;]", text)):
        parsed = _parse_clause(clause, today)
        if parsed is None:
            return []
        actions += parsed
    return actions


def _posted_briefs(briefs_dir, oldest):
    briefs = []
    for path in Path(briefs_dir).glob("*.posted.json"):
        ts = json.loads(path.read_text())["ts"]
        if float(ts) >= oldest.timestamp():
            briefs.append((ts, path.name.removesuffix(".posted.json")))
    return sorted(briefs, key=lambda b: float(b[0]))


def _brief_for(message, briefs):
    by_ts = dict(briefs)
    if message.get("thread_ts") in by_ts:
        return by_ts[message["thread_ts"]]
    earlier = [name for ts, name in briefs if float(ts) < float(message["ts"])]
    return earlier[-1] if earlier else None


def _items(briefs_dir, brief):
    path = Path(briefs_dir) / f"{brief}.items.json"
    return {item["n"]: item["text"] for item in json.loads(path.read_text())} if brief and path.exists() else {}


def _apply(message, brief, items, now):
    """Returns (log rows, confirmation text) for one reply."""
    actions = parse(message.get("text", ""), now.date())
    if not actions:
        return [], HELP
    rows, notes = [], []
    for action in actions:
        if action["action"] == "add":
            item = action["text"]
            notes.append(f"➕ {item}")
        else:
            item = items.get(action["n"])
            if item is None:
                notes.append(f"❓ no item {action['n']} in {brief or 'any recent brief'}")
                continue
            mark = {"done": "✅", "drop": "🗑", "later": "🗓"}[action["action"]]
            when = f" → {date.fromisoformat(action['until']):%a %d %b}" if action["action"] == "later" else ""
            notes.append(f"{mark} {action['n']} {item}{when}")
        rows.append({"at": now.isoformat(), "brief": brief, "msg_ts": message["ts"], "action": action["action"],
                     "n": action.get("n"), "item": item, "until": action.get("until")})
    return rows, ("Got it · " if rows else "") + " · ".join(notes)


def poll(token, channel, briefs_dir, state_dir, now):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_path = state_dir / "seen.json"
    seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    oldest = now - LOOKBACK
    briefs = _posted_briefs(briefs_dir, oldest)

    window = {"channel": channel, "oldest": f"{oldest.timestamp():.6f}", "limit": 200}
    messages = slack_post._call("conversations.history", token, window)["messages"]
    for ts, _ in briefs:
        thread = slack_post._call("conversations.replies", token, {**window, "ts": ts})["messages"]
        messages += [m for m in thread if m["ts"] != ts]

    replies = {m["ts"]: m for m in messages
               if m.get("user") == slack_post.USER and not m.get("bot_id") and not m.get("subtype")}
    with open(state_dir / "actions.jsonl", "a") as log:
        for ts in sorted(replies, key=float):
            if ts in seen:
                continue
            message = replies[ts]
            brief = _brief_for(message, briefs)
            rows, confirmation = _apply(message, brief, _items(briefs_dir, brief), now)
            log.writelines(json.dumps(row) + "\n" for row in rows)
            slack_post.post(token, channel, confirmation, thread_ts=message.get("thread_ts") or ts)
            # marked only after the confirmation went out, so a failed post is retried next poll
            seen.add(ts)
            seen_path.write_text(json.dumps(sorted(seen, key=float)[-500:]))


def summary(state_dir, since, today):
    path = Path(state_dir) / "actions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    recent = [r for r in rows if datetime.fromisoformat(r["at"]) >= since]
    sections = {
        "Done": [r["item"] for r in recent if r["action"] == "done"],
        "Dropped": [r["item"] for r in recent if r["action"] == "drop"],
        "Snoozed (leave out until the date)": [f"{r['item']} → {r['until']}" for r in rows
                                               if r["action"] == "later" and r["until"] > today.isoformat()],
        "Back today": [r["item"] for r in rows if r["action"] == "later"
                       and since.date().isoformat() <= r["until"] <= today.isoformat()],
        "Added by hand": [r["item"] for r in recent if r["action"] == "add"],
    }
    lines = [f"{label}: " + "; ".join(items) for label, items in sections.items() if items]
    dead = [f for f in (Path(state_dir) / "outbox" / "dead").glob("*.json") if f.stat().st_mtime >= since.timestamp()]
    if dead:
        lines.append(f"Undelivered: {len(dead)} message(s) failed for 24h+ and were given up (state/outbox/dead)")
    return "\n".join(lines) or f"No replies to the briefs since {since:%d %b %H:%M}."


def missing_brief(briefs_dir, now):
    """The brief that should exist by now but doesn't: Desktop never retries a run that failed offline."""
    clock, day = now.time(), f"{now:%Y-%m-%d}"
    for kind, opens, closes in (("am", time(10, 30), time(13, 0)), ("pm", time(18, 30), time(22, 0))):
        if opens <= clock < closes and not (Path(briefs_dir) / f"{day}-{kind}.md").exists():
            return kind
    return None


def poll_due(state_dir, now):
    path = Path(state_dir) / "last_poll.txt"
    return not path.exists() or now - datetime.fromisoformat(path.read_text().strip()) >= POLL_EVERY


def brief_tools():
    tools = [f"Bash(python3 {HERE / script}:*)" for script in ("cc_digest.py", "actions.py", "slack_post.py")]
    # an Edit rule (headless checks ignore Write(path) rules) with a // prefix: a single leading /
    # in a rule is not an absolute path, and both mistakes silently blocked the brief file (24-26 Oct)
    return tools + [f"Edit(/{HERE}/briefs/**)", f"Read(/{HERE}/**)", "Glob", *CONNECTOR_READ_TOOLS, *TICKTICK_TOOLS]


def run_brief(kind, state_dir):
    """Runs a missed brief headless with the same instructions and only the tools it needs."""
    prompt = HERE / "prompts" / ("morning.md" if kind == "am" else "evening.md")
    claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
    with open(Path(state_dir) / f"last-watchdog-{kind}.log", "w") as log:
        subprocess.run([claude, "-p", f"Read {prompt} and follow it exactly.", "--model", "haiku",
                        "--allowedTools", ",".join(brief_tools()), "--disable-slash-commands", "--no-chrome",
                        "--no-session-persistence"], cwd=HERE, stdout=log, stderr=log, timeout=1800, check=False)


def tick_due(state_dir, now):
    """Hourly, as asked; a network-change trigger in between only matters when messages are waiting."""
    if any((Path(state_dir) / "outbox").glob("*.json")):
        return True
    path = Path(state_dir) / "last_tick.txt"
    return not path.exists() or now - datetime.fromisoformat(path.read_text().strip()) >= POLL_EVERY


def tick(now):
    """The hourly launchd job: deliver the outbox, read replies, and run any brief that never happened."""
    briefs, state = HERE / "briefs", HERE / "state"
    if not tick_due(state, now):
        return
    (state / "last_tick.txt").write_text(now.isoformat())
    try:
        token = slack_post.load_token(slack_post.TOKEN_PATH)
        channel = slack_post.dm_channel(token, slack_post.USER)
        print(f"{now:%Y-%m-%d %H:%M} outbox sent {{}}, queued {{}}, gave up {{}}".format(
            *slack_post.flush(token, channel, slack_post.OUTBOX, now)))
    except (slack_post.SlackError, OSError) as error:
        print(f"{now:%Y-%m-%d %H:%M} slack unreachable, retrying next hour: {error}")
        return
    try:
        if poll_due(state, now):
            poll(token, channel, briefs, state, now)
            (state / "last_poll.txt").write_text(now.isoformat())
    except (slack_post.SlackError, OSError) as error:
        # a reply-reading problem (e.g. a missing scope) must not stop a missed brief from running
        print(f"{now:%Y-%m-%d %H:%M} replies not read: {error}")
    kind = missing_brief(briefs, now)
    if kind:
        print(f"{now:%Y-%m-%d %H:%M} {kind} brief missing, running it now")
        run_brief(kind, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("poll")
    commands.add_parser("tick", help="hourly job: outbox, replies, missed briefs")
    commands.add_parser("summary").add_argument("--kind", choices=["am", "pm"], required=True)
    args = parser.parse_args()
    now = datetime.now(IST)
    briefs, state = HERE / "briefs", HERE / "state"
    if args.command == "summary":
        print(summary(state, since_for(args.kind, briefs, now), now.date()))
        return
    if args.command == "tick":
        tick(now)
        return
    token = slack_post.load_token(slack_post.TOKEN_PATH)
    try:
        poll(token, slack_post.dm_channel(token, slack_post.USER), briefs, state, now)
    except (slack_post.SlackError, OSError) as error:
        raise SystemExit(f"replies not read, Slack unreachable: {error}")


if __name__ == "__main__":
    main()
