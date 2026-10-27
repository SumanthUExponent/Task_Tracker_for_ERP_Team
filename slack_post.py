"""Send the daily briefs and their reminders to you as the Daily Brief bot.

A bot sender is the point: a message the Slack connector posts is sent as you,
and Slack does not notify anyone of their own messages.
"""
import argparse
import json
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")
USER = config.load().get("SLACK_USER_ID", "")
TOKEN_PATH = config.CONFIG_PATH
CHUNK_CHARS = 3_500
THREAD_MARKER = "--- thread ---"
OUTBOX = Path(__file__).resolve().parent / "state" / "outbox"
LATE_AFTER = timedelta(minutes=15)
REMINDER_GRACE = timedelta(hours=3)
GIVE_UP_AFTER = timedelta(hours=24)
# Slack answers invalid_arguments when these read methods get a JSON body (seen 23 Oct 2026)
FORM_ONLY_METHODS = {"conversations.history", "conversations.replies"}


class SlackError(Exception):
    pass


def load_token(path):
    try:
        for line in Path(path).read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "SLACK_BOT_TOKEN" and value.strip():
                return value.strip()
    except FileNotFoundError:
        pass
    raise SystemExit(f"slack token missing: {path}")


def _call(method, token, payload):
    headers = {"Authorization": f"Bearer {token}"}
    if method in FORM_ONLY_METHODS:
        data = urllib.parse.urlencode(payload).encode()
    else:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if not body.get("ok"):
        raise SlackError(body.get("error", "unknown_error"))
    return body


def dm_channel(token, user):
    if not user:
        config.require({}, "SLACK_USER_ID")
    return _call("conversations.open", token, {"users": user})["channel"]["id"]


def _chunks(text):
    """Packs whole paragraphs into messages so a brief never splits mid-item."""
    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= CHUNK_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > CHUNK_CHARS:
            chunks.append(paragraph[:CHUNK_CHARS])
            paragraph = paragraph[CHUNK_CHARS:]
        current = paragraph
    return chunks + [current] if current else chunks


def post(token, channel, text, thread_ts=None):
    if not text.strip():
        raise ValueError("brief is empty, nothing posted")
    # previews of Granola/Gmail links would bury a 30-second brief under cards
    payload = {"channel": channel, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return [_call("chat.postMessage", token, {**payload, "text": chunk})["ts"] for chunk in _chunks(text)]


def _post_at(at_local, now):
    at = datetime.fromisoformat(at_local).replace(tzinfo=IST)
    if at <= (now or datetime.now(IST)):
        raise ValueError(f"reminder time {at_local} is not in the future")
    return int(at.timestamp())


def schedule(token, channel, text, at_local, now=None):
    post_at = _post_at(at_local, now)
    return _call("chat.scheduleMessage", token, {"channel": channel, "text": text, "post_at": post_at})["scheduled_message_id"]


def _split_brief(text):
    summary, _, details = text.partition(f"\n{THREAD_MARKER}\n")
    return summary.strip(), details.strip()


def deliver_brief(token, channel, path, outbox=None, now=None, delayed=False):
    """Posts a brief file and records its ts; with an outbox, a failed thread reply is queued, not lost."""
    summary, details = _split_brief(Path(path).read_text())
    if delayed:
        summary = "⏳ Delayed · " + summary
    stamps = post(token, channel, summary)
    # actions.py maps a thread reply ("done 2") back to this brief through its ts
    Path(path).with_suffix(".posted.json").write_text(json.dumps({"ts": stamps[0]}))
    if details:
        try:
            stamps += post(token, channel, details, thread_ts=stamps[0])
        except (SlackError, OSError):
            if outbox is None:
                raise
            enqueue(outbox, {"kind": "thread", "text": details, "thread_ts": stamps[0]}, now)
    return stamps


def enqueue(outbox, job, now):
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    job = {**job, "created": now.isoformat(), "attempts": 0}
    (outbox / f"{now:%Y%m%dT%H%M%S}-{job['kind']}-{uuid.uuid4().hex[:6]}.json").write_text(json.dumps(job))


def _deliver_job(token, channel, job, outbox, now):
    """Sends one queued job; returns False when it is no longer worth sending."""
    created = datetime.fromisoformat(job["created"])
    if job["kind"] == "brief":
        deliver_brief(token, channel, job["file"], outbox, now, delayed=now - created > LATE_AFTER)
    elif job["kind"] == "thread":
        post(token, channel, job["text"], thread_ts=job["thread_ts"])
    elif job["kind"] == "remind":
        at = datetime.fromisoformat(job["at"]).replace(tzinfo=IST)
        if at > now + timedelta(minutes=1):
            schedule(token, channel, job["text"], job["at"], now)
        elif now - at <= REMINDER_GRACE:
            post(token, channel, "⏰ (late) " + job["text"].removeprefix("⏰").strip())
        else:
            return False
    return True


def flush(token, channel, outbox, now):
    """Retries every queued job once, oldest first; returns (sent, still queued, given up)."""
    outbox = Path(outbox)
    sent = kept = dead = 0
    for path in sorted(outbox.glob("*.json")):
        job = json.loads(path.read_text())
        try:
            delivered = _deliver_job(token, channel, job, outbox, now)
        except (SlackError, OSError) as error:
            job["attempts"] += 1
            job["last_error"] = str(error)
            if now - datetime.fromisoformat(job["created"]) <= GIVE_UP_AFTER:
                path.write_text(json.dumps(job))
                kept += 1
                continue
            delivered = False
        if delivered:
            path.unlink()
            sent += 1
        else:
            (outbox / "dead").mkdir(exist_ok=True)
            path.rename(outbox / "dead" / path.name)
            dead += 1
    return sent, kept, dead


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("post").add_argument("--file", type=Path, required=True)
    remind = commands.add_parser("remind")
    remind.add_argument("--at", required=True, help="YYYY-MM-DDTHH:MM, Asia/Kolkata")
    # a file, never an inline argument: reminder text is quoted from Slack and email, and
    # inside a shell command line `$(…)` or backticks in it would execute
    remind.add_argument("--text-file", type=Path, required=True)
    commands.add_parser("flush", help="retry everything waiting in the outbox")
    args = parser.parse_args(argv)
    now = now or datetime.now(IST)

    try:
        if args.command == "remind":
            _post_at(args.at, now)
            text = args.text_file.read_text().strip()
            if not text:
                raise ValueError("reminder text is empty")
        elif args.command == "post" and not args.file.read_text().strip():
            raise ValueError("brief is empty, nothing posted")
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))

    token = load_token(TOKEN_PATH)
    try:
        channel = dm_channel(token, USER)
        if args.command == "post":
            print(f"posted {len(deliver_brief(token, channel, args.file, OUTBOX, now))} message(s)")
        elif args.command == "remind":
            print(f"scheduled {schedule(token, channel, text, args.at, now)} at {args.at}")
        else:
            print("sent {}, still queued {}, gave up {}".format(*flush(token, channel, OUTBOX, now)))
    except (SlackError, OSError) as error:
        if args.command == "flush":
            raise SystemExit(f"outbox not flushed, will retry: {error}")
        job = {"kind": "brief", "file": str(args.file.resolve())} if args.command == "post" else \
            {"kind": "remind", "text": text, "at": args.at}
        enqueue(OUTBOX, job, now)
        print(f"queued for retry ({error}); it is sent automatically once Slack is reachable")


if __name__ == "__main__":
    main()
