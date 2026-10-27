import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cc_digest import MAX_CHARS, build_digest, since_for, window_header

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=IST)
SINCE = NOW - timedelta(hours=24)
ECR_CWD = "/Users/x/code/apps/acme_app"


def iso(dt):
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(text, at, session="s1", cwd=ECR_CWD, **extra):
    return {"type": "user", "timestamp": iso(at), "sessionId": session, "cwd": cwd,
            "message": {"role": "user", "content": text}, **extra}


def assistant(text, at, session="s1", cwd=ECR_CWD):
    return {"type": "assistant", "timestamp": iso(at), "sessionId": session, "cwd": cwd,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, raw_tail=""):
        project_dir = self.projects / "-Users-x-proj"
        project_dir.mkdir(exist_ok=True)
        path = project_dir / f"{len(list(project_dir.iterdir()))}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n" + raw_tail)
        return path

    def digest(self):
        return build_digest(self.projects, SINCE, NOW)

    def test_keeps_human_prompts_only(self):
        at = NOW - timedelta(hours=1)
        self.write([
            user("fix the checkout bug", at),
            user("sidechain prompt", at, isSidechain=True),
            user("meta prompt", at, isMeta=True),
            user("<command-name>/mcp</command-name>", at),
            {"type": "user", "timestamp": iso(at), "sessionId": "s1", "cwd": ECR_CWD,
             "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool output"}]}},
        ])
        out = self.digest()
        self.assertIn("fix the checkout bug", out)
        for noise in ("sidechain prompt", "meta prompt", "/mcp", "tool output"):
            self.assertNotIn(noise, out)

    def test_reads_text_block_prompts(self):
        blocks = [{"type": "text", "text": "<browser_instruction># Claude in Chrome</browser_instruction>"},
                  {"type": "text", "text": "check why MR notifications are not sending"}]
        self.write([user(blocks, NOW - timedelta(hours=1))])
        out = self.digest()
        self.assertIn("check why MR notifications are not sending", out)
        self.assertNotIn("browser_instruction", out)

    def test_pasted_content_kept_as_paste(self):
        pasted = '  <pasted_content id="d738">\nLV SAM MAPPING DELTA - 2026-10-21\nrow 1\n</pasted_content id="d738">'
        self.write([user([{"type": "text", "text": pasted}], NOW - timedelta(hours=1))])
        self.assertIn("[pasted] LV SAM MAPPING DELTA - 2026-10-21", self.digest())

    def test_drops_interrupt_marker(self):
        self.write([user([{"type": "text", "text": "[Request interrupted by user]"}], NOW - timedelta(hours=1))])
        self.assertNotIn("interrupted", self.digest())

    def test_tool_only_session_in_same_project(self):
        at = NOW - timedelta(hours=1)
        tool_only = {"type": "user", "timestamp": iso(at), "sessionId": "s2", "cwd": ECR_CWD,
                     "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}
        self.write([user("real prompt", at), tool_only])
        self.assertIn("real prompt", self.digest())

    def test_odd_records_skipped(self):
        at = NOW - timedelta(hours=1)
        self.write([123, {"type": "user", "timestamp": "garbage", "message": {"content": "bad"}},
                    {"type": "user", "timestamp": 5, "message": {"content": "bad"}}, user("good one", at)])
        self.assertIn("good one", self.digest())

    def test_window_uses_record_timestamp(self):
        self.write([
            user("too old", SINCE - timedelta(minutes=1)),
            user("just inside", SINCE + timedelta(minutes=1)),
        ])
        out = self.digest()
        self.assertNotIn("too old", out)
        self.assertIn("just inside", out)

    def test_groups_by_cwd_project(self):
        self.write([user("hello", NOW - timedelta(hours=1))])
        self.assertIn("## apps/acme_app", self.digest())

    def test_appends_last_assistant_text(self):
        at = NOW - timedelta(hours=2)
        self.write([
            user("do it", at),
            assistant("first answer", at + timedelta(minutes=1)),
            assistant("final answer", at + timedelta(minutes=2)),
        ])
        out = self.digest()
        self.assertIn("→ final answer", out)
        self.assertNotIn("first answer", out)

    def test_skips_malformed_lines(self):
        self.write([user("survives", NOW - timedelta(hours=1))], raw_tail='{"type":"us')
        self.assertIn("survives", self.digest())

    def test_digest_respects_cap(self):
        start = SINCE + timedelta(minutes=1)
        self.write([user(f"p{i:04d} " + "x" * 295, start + timedelta(seconds=30 * i)) for i in range(2000)])
        out = self.digest()
        self.assertLessEqual(len(out), MAX_CHARS)
        self.assertRegex(out, r"\(\+\d+ more\)")
        self.assertIn("p1999", out)


class WindowHeaderTest(unittest.TestCase):
    def test_am_late_after_noon(self):
        late = datetime(2026, 10, 22, 13, 5, tzinfo=IST)
        self.assertEqual(window_header("am", late - timedelta(hours=28), late),
                         "Window: 21 Oct 09:05 → 22 Oct 13:05 IST (late run: yes)")

    def test_am_on_time(self):
        self.assertTrue(window_header("am", SINCE, NOW).endswith("(late run: no)"))

    def test_pm_has_no_late_flag(self):
        evening = datetime(2026, 10, 22, 18, 0, tzinfo=IST)
        self.assertEqual(window_header("pm", evening - timedelta(hours=42), evening),
                         "Window: 21 Oct 00:00 → 22 Oct 18:00 IST")


class WeeklyTest(unittest.TestCase):
    MONDAY = datetime(2026, 10, 26, 9, 0, tzinfo=IST)
    FRIDAY = datetime(2026, 10, 23, 18, 0, tzinfo=IST)

    def test_monday_morning_covers_last_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "2026-10-25-am.md").write_text("sunday brief")
            self.assertEqual(since_for("am", Path(tmp), self.MONDAY), datetime(2026, 10, 19, 0, 0, tzinfo=IST))

    def test_friday_evening_stays_daily(self):
        self.assertEqual(since_for("pm", Path("/nonexistent"), self.FRIDAY), datetime(2026, 10, 22, 0, 0, tzinfo=IST))

    def test_weekly_header(self):
        self.assertEqual(window_header("am", datetime(2026, 10, 19, 0, 0, tzinfo=IST), self.MONDAY),
                         "Window: 19 Oct 00:00 → 26 Oct 09:00 IST (late run: no) (weekly)")
        self.assertEqual(window_header("pm", datetime(2026, 10, 22, 0, 0, tzinfo=IST), self.FRIDAY),
                         "Window: 22 Oct 00:00 → 23 Oct 18:00 IST")


class SinceForTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.briefs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def brief_at(self, name, at):
        path = self.briefs / name
        path.write_text("brief")
        os.utime(path, (at.timestamp(), at.timestamp()))

    def test_since_am_uses_last_brief(self):
        self.brief_at("2026-10-21-am.md", NOW - timedelta(hours=30))
        self.brief_at("2026-10-21-pm.md", NOW - timedelta(hours=2))
        self.assertEqual(since_for("am", self.briefs, NOW), NOW - timedelta(hours=30))

    def test_since_am_caps_at_72h(self):
        self.assertEqual(since_for("am", self.briefs, NOW), NOW - timedelta(hours=24))
        self.brief_at("2026-10-17-am.md", NOW - timedelta(days=5))
        self.assertEqual(since_for("am", self.briefs, NOW), NOW - timedelta(hours=72))

    def test_since_pm_is_yesterday_midnight_ist(self):
        now = datetime(2026, 10, 22, 18, 0, tzinfo=IST)
        self.assertEqual(since_for("pm", self.briefs, now), datetime(2026, 10, 21, 0, 0, tzinfo=IST))


if __name__ == "__main__":
    unittest.main()
