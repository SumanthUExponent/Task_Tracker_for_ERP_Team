import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import actions
import slack_post
from actions import brief_tools, missing_brief, parse, poll, poll_due, summary, tick_due

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 10, 22)  # a Thursday
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=IST)
USER = slack_post.USER


class ParseTest(unittest.TestCase):
    def test_done_numbers_any_order(self):
        expected = [{"action": "done", "n": 1}, {"action": "done", "n": 3}]
        for text in ("done 1 3", "Done 1,3", "1 3 done", "✅ 1 3"):
            self.assertEqual(parse(text, TODAY), expected, text)

    def test_drop(self):
        self.assertEqual(parse("drop 2", TODAY), [{"action": "drop", "n": 2}])

    def test_snooze_to_weekday(self):
        self.assertEqual(parse("2 fri", TODAY), [{"action": "later", "n": 2, "until": "2026-10-23"}])
        self.assertEqual(parse("snooze 4 thursday", TODAY), [{"action": "later", "n": 4, "until": "2026-10-29"}])

    def test_snooze_defaults_and_dates(self):
        self.assertEqual(parse("later 5", TODAY), [{"action": "later", "n": 5, "until": "2026-10-23"}])
        self.assertEqual(parse("5 next week", TODAY), [{"action": "later", "n": 5, "until": "2026-10-26"}])
        self.assertEqual(parse("5 2026-11-02", TODAY), [{"action": "later", "n": 5, "until": "2026-11-02"}])

    def test_add_keeps_original_text(self):
        self.assertEqual(parse("add Call Ben re budget", TODAY), [{"action": "add", "text": "Call Ben re budget"}])

    def test_several_commands(self):
        self.assertEqual(parse("done 1\ndrop 2; 3 tomorrow", TODAY), [
            {"action": "done", "n": 1}, {"action": "drop", "n": 2},
            {"action": "later", "n": 3, "until": "2026-10-23"}])

    def test_not_a_command(self):
        self.assertEqual(parse("thanks, looks great", TODAY), [])
        self.assertEqual(parse("done", TODAY), [])


class FakeSlack:
    def __init__(self, history=(), replies=None):
        self.history, self.replies, self.posted = list(history), replies or {}, []

    def __call__(self, method, token, payload):
        if method == "conversations.history":
            return {"ok": True, "messages": self.history}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies.get(payload["ts"], [])}
        if method == "chat.postMessage":
            self.posted.append(payload)
            return {"ok": True, "ts": f"9{len(self.posted)}.0"}
        raise AssertionError(method)


def msg(ts, text, thread_ts=None, user=USER, **extra):
    return {"ts": ts, "text": text, "user": user, **({"thread_ts": thread_ts} if thread_ts else {}), **extra}


class PollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.briefs = Path(self.tmp.name) / "briefs"
        self.state = Path(self.tmp.name) / "state"
        self.briefs.mkdir()
        brief_ts = str(NOW.replace(hour=9).timestamp())
        self.brief_ts = brief_ts
        (self.briefs / "2026-10-22-am.posted.json").write_text(json.dumps({"ts": brief_ts}))
        (self.briefs / "2026-10-22-am.items.json").write_text(json.dumps([
            {"n": 1, "text": "Decide TICKET-101"}, {"n": 2, "text": "Send server-7 ticket"},
            {"n": 3, "text": "Plan budget release"}]))

    def run_poll(self, fake):
        with mock.patch.object(slack_post, "_call", fake):
            poll("xoxb-t", "D1", self.briefs, self.state, NOW)

    def logged(self):
        path = self.state / "actions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_thread_reply_marks_done_and_confirms(self):
        reply_ts = str(NOW.replace(hour=10).timestamp())
        fake = FakeSlack(history=[{"ts": self.brief_ts, "bot_id": "B1", "text": "brief", "reply_count": 1}],
                         replies={self.brief_ts: [msg(reply_ts, "done 1 3", thread_ts=self.brief_ts)]})
        self.run_poll(fake)
        self.assertEqual([(a["action"], a["item"]) for a in self.logged()],
                         [("done", "Decide TICKET-101"), ("done", "Plan budget release")])
        self.assertEqual(len(fake.posted), 1)
        self.assertEqual(fake.posted[0]["thread_ts"], self.brief_ts)
        self.assertIn("✅ 1 Decide TICKET-101", fake.posted[0]["text"])

    def test_same_message_not_applied_twice(self):
        reply_ts = str(NOW.replace(hour=10).timestamp())
        history = [msg(reply_ts, "drop 2")]
        self.run_poll(FakeSlack(history=history))
        second = FakeSlack(history=history)
        self.run_poll(second)
        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(second.posted, [])

    def test_top_level_message_uses_latest_brief(self):
        self.run_poll(FakeSlack(history=[msg(str(NOW.replace(hour=10).timestamp()), "2 fri")]))
        self.assertEqual(self.logged()[0]["item"], "Send server-7 ticket")
        self.assertEqual(self.logged()[0]["until"], "2026-10-23")

    def test_unknown_number_and_unparsed_text_get_help(self):
        fake = FakeSlack(history=[msg("1792650000.1", "done 9"), msg("1792650000.2", "hello there")])
        self.run_poll(fake)
        self.assertEqual(self.logged(), [])
        self.assertIn("no item 9", fake.posted[0]["text"])
        self.assertIn("done 1 3", fake.posted[1]["text"])

    def test_ignores_bot_and_other_users(self):
        fake = FakeSlack(history=[msg("1792650000.1", "done 1", bot_id="B1"), msg("1792650000.2", "done 1", user="U999")])
        self.run_poll(fake)
        self.assertEqual(self.logged(), [])
        self.assertEqual(fake.posted, [])


class SummaryTest(unittest.TestCase):
    def test_groups_actions_since_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            rows = [
                {"at": "2026-10-21T08:00:00+05:30", "action": "done", "item": "Old thing"},
                {"at": "2026-10-22T10:00:00+05:30", "action": "done", "item": "Decide TICKET-101"},
                {"at": "2026-10-22T10:01:00+05:30", "action": "drop", "item": "Run MR script"},
                {"at": "2026-10-22T10:02:00+05:30", "action": "later", "item": "Plan budget", "until": "2026-10-24"},
                {"at": "2026-10-20T10:00:00+05:30", "action": "later", "item": "Back now", "until": "2026-10-22"},
                {"at": "2026-10-22T10:03:00+05:30", "action": "add", "item": "Call Ben"},
            ]
            (state / "actions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            out = summary(state, datetime(2026, 10, 21, 9, 0, tzinfo=IST), TODAY)
        self.assertIn("Done: Decide TICKET-101", out)
        self.assertNotIn("Old thing", out)
        self.assertIn("Dropped: Run MR script", out)
        self.assertIn("Snoozed (leave out until the date): Plan budget → 2026-10-24", out)
        self.assertIn("Back today: Back now", out)
        self.assertIn("Added by hand: Call Ben", out)

    def test_reports_undelivered_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            dead = Path(tmp) / "outbox" / "dead"
            dead.mkdir(parents=True)
            (dead / "20261022T090000-brief-abc.json").write_text("{}")
            out = summary(Path(tmp), datetime(2026, 10, 21, 9, 0, tzinfo=IST), TODAY)
        self.assertIn("Undelivered: 1 message", out)


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.briefs = Path(self.tmp.name)

    def at(self, hour, minute=0):
        return datetime(2026, 10, 23, hour, minute, tzinfo=IST)

    def test_morning_missing_after_1030(self):
        self.assertIsNone(missing_brief(self.briefs, self.at(10, 0)))
        self.assertEqual(missing_brief(self.briefs, self.at(10, 45)), "am")
        self.assertIsNone(missing_brief(self.briefs, self.at(13, 30)))

    def test_existing_brief_is_never_rerun(self):
        (self.briefs / "2026-10-23-am.md").write_text("x")
        self.assertIsNone(missing_brief(self.briefs, self.at(11, 0)))

    def test_evening_missing_after_1830(self):
        self.assertEqual(missing_brief(self.briefs, self.at(19, 0)), "pm")
        (self.briefs / "2026-10-23-pm.md").write_text("x")
        self.assertIsNone(missing_brief(self.briefs, self.at(19, 0)))

    def test_tick_runs_hourly_unless_messages_wait(self):
        state, now = self.briefs, self.at(11, 0)
        self.assertTrue(tick_due(state, now))
        (state / "last_tick.txt").write_text((now - timedelta(minutes=10)).isoformat())
        self.assertFalse(tick_due(state, now))
        (state / "outbox").mkdir()
        (state / "outbox" / "20261023T090000-brief-abc.json").write_text("{}")
        self.assertTrue(tick_due(state, now))

    def test_headless_brief_may_edit_only_briefs(self):
        tools = brief_tools()
        self.assertIn(f"Edit(/{actions.HERE}/briefs/**)", tools)
        self.assertFalse(any(t.startswith("Write(") for t in tools))

    def test_headless_brief_can_sync_ticktick_but_not_delete_lists(self):
        tools = brief_tools()
        self.assertIn("mcp__claude_ai_TickTick__batch_add_tasks", tools)
        self.assertIn("mcp__claude_ai_TickTick__list_completed_tasks_by_date", tools)
        self.assertFalse(any("delete_project" in t or "habit" in t for t in tools))

    def test_poll_due_hourly(self):
        state = self.briefs
        now = self.at(11, 0)
        self.assertTrue(poll_due(state, now))
        (state / "last_poll.txt").write_text((now - timedelta(minutes=20)).isoformat())
        self.assertFalse(poll_due(state, now))
        (state / "last_poll.txt").write_text((now - timedelta(minutes=56)).isoformat())
        self.assertTrue(poll_due(state, now))


if __name__ == "__main__":
    unittest.main()
