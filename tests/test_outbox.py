import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import slack_post
from slack_post import THREAD_MARKER, deliver_brief, enqueue, flush

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=IST)


class FakeSlack:
    """Records calls; `down` makes every call fail like a dropped network."""

    def __init__(self, down=False, fail_thread=False):
        self.down, self.fail_thread, self.calls = down, fail_thread, []

    def __call__(self, method, token, payload):
        if self.down or (self.fail_thread and payload.get("thread_ts")):
            raise urllib.error.URLError("network is unreachable")
        self.calls.append((method, payload))
        return {"ok": True, "ts": f"{len(self.calls)}.0", "scheduled_message_id": "Q1"}


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.outbox = self.dir / "outbox"
        self.brief = self.dir / "2026-10-23-am.md"
        self.brief.write_text(f"3 must-dos · start with X\n🔴 1 *Decide* X\n{THREAD_MARKER}\n*Also today*\n4. Y")

    def jobs(self, folder=None):
        return sorted((folder or self.outbox).glob("*.json"))

    def run_flush(self, fake, now):
        with mock.patch.object(slack_post, "_call", fake):
            return flush("xoxb-t", "D1", self.outbox, now)

    def test_offline_brief_is_queued_then_sent_marked_delayed(self):
        with mock.patch.object(slack_post, "_call", FakeSlack(down=True)), self.assertRaises(OSError):
            deliver_brief("xoxb-t", "D1", self.brief)
        enqueue(self.outbox, {"kind": "brief", "file": str(self.brief)}, NOW)
        fake = FakeSlack()
        self.run_flush(fake, NOW + timedelta(hours=2))
        self.assertEqual(self.jobs(), [])
        parent = fake.calls[0][1]
        self.assertTrue(parent["text"].startswith("⏳ Delayed · 3 must-dos"))
        self.assertEqual(fake.calls[1][1]["thread_ts"], "1.0")
        self.assertEqual(json.loads((self.dir / "2026-10-23-am.posted.json").read_text()), {"ts": "1.0"})

    def test_thread_failure_requeues_only_the_thread(self):
        fake = FakeSlack(fail_thread=True)
        with mock.patch.object(slack_post, "_call", fake):
            deliver_brief("xoxb-t", "D1", self.brief, outbox=self.outbox, now=NOW)
        self.assertEqual(len(fake.calls), 1)
        job = json.loads(self.jobs()[0].read_text())
        self.assertEqual((job["kind"], job["thread_ts"]), ("thread", "1.0"))
        retry = FakeSlack()
        self.run_flush(retry, NOW + timedelta(hours=1))
        self.assertEqual(retry.calls[0][1]["thread_ts"], "1.0")
        self.assertEqual(self.jobs(), [])

    def test_still_offline_keeps_job(self):
        enqueue(self.outbox, {"kind": "thread", "text": "hi", "thread_ts": "1.0"}, NOW)
        self.run_flush(FakeSlack(down=True), NOW + timedelta(hours=1))
        self.assertEqual(len(self.jobs()), 1)

    def test_gives_up_after_24h_into_dead_letters(self):
        enqueue(self.outbox, {"kind": "thread", "text": "hi", "thread_ts": "1.0"}, NOW)
        self.run_flush(FakeSlack(down=True), NOW + timedelta(hours=25))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(len(self.jobs(self.outbox / "dead")), 1)

    def test_reminder_future_is_scheduled(self):
        enqueue(self.outbox, {"kind": "remind", "text": "⏰ Plan budget", "at": "2026-10-23T15:00"}, NOW)
        fake = FakeSlack()
        self.run_flush(fake, NOW + timedelta(hours=1))
        self.assertEqual(fake.calls[0][0], "chat.scheduleMessage")

    def test_reminder_recently_missed_is_sent_late(self):
        enqueue(self.outbox, {"kind": "remind", "text": "⏰ Plan budget", "at": "2026-10-23T09:30"}, NOW)
        fake = FakeSlack()
        self.run_flush(fake, NOW + timedelta(hours=2))
        self.assertEqual(fake.calls[0][0], "chat.postMessage")
        self.assertTrue(fake.calls[0][1]["text"].startswith("⏰ (late) "))

    def test_reminder_long_missed_is_dropped(self):
        enqueue(self.outbox, {"kind": "remind", "text": "⏰ Plan budget", "at": "2026-10-23T09:30"}, NOW)
        fake = FakeSlack()
        self.run_flush(fake, NOW + timedelta(hours=5))
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.jobs(self.outbox / "dead")), 1)


class MainQueuesTest(unittest.TestCase):
    def test_post_offline_queues_and_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "2026-10-23-am.md"
            brief.write_text("3 must-dos")
            outbox = Path(tmp) / "outbox"
            with mock.patch.object(slack_post, "load_token", return_value="xoxb-t"), \
                    mock.patch.object(slack_post, "USER", "U_TEST"), \
                    mock.patch.object(slack_post, "OUTBOX", outbox), \
                    mock.patch.object(slack_post, "_call", FakeSlack(down=True)), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                slack_post.main(["post", "--file", str(brief)], now=NOW)
            self.assertIn("queued", out.getvalue())
            self.assertEqual(json.loads(next(outbox.glob("*.json")).read_text())["kind"], "brief")


if __name__ == "__main__":
    unittest.main()
