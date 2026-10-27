import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import granola_archive as ga

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 23, 21, 7, tzinfo=IST)
MID = "00000000-0000-4000-8000-000000000001"

MEETINGS_TEXT = f"""<connected_user_email>asha@example.com</connected_user_email>
<meetings_data from="Oct 21, 2026" to="Oct 21, 2026" count="1">
<meeting id="{MID}" title="Purchase approvals - SOP" date="Oct 21, 2026 12:30 PM GMT+5:30" meeting_at="2026-10-21T12:30:00.000+05:30" created_at="2026-10-21T12:31:58.458+05:30" updated_at="2026-10-21T13:23:41.555+05:30" url="https://notes.granola.ai/d/{MID}">
  <known_participants total="11" omitted="6">
  Asha Rao (note creator) from Acme (asha@example.com), Ben (ben@example.com), Chen Li from Acme (chen@example.com)
  </known_participants>
    <folders />
  <summary>
# R\\&amp;D Budget

- Two-stage approach agreed
</summary>
  <attachments>
<attachment id="c8d4" type="screenshot" captured_at="2026-10-21T07:17:55.491Z">Sheet “RnD budget &lt;19102026&gt;” is open.</attachment>
</attachments>
</meeting>
</meetings_data>"""

LIST_TEXT = f"""<meetings_data count="2">
<meeting id="{MID}" title="Purchase approvals - SOP" date="Oct 21, 2026 12:30 PM GMT+5:30" meeting_at="2026-10-21T12:30:00.000+05:30" url="https://notes.granola.ai/d/{MID}">
</meeting>
<meeting id="00000000-0000-4000-8000-000000000002" title="Vendor review" meeting_at="2026-10-21T15:12:20.899+05:30" url="x">
</meeting>
</meetings_data>"""


def transcript_page(offset, text, next_offset=None, total=None):
    page = {"id": MID, "offset": offset, "transcript": text, "truncated": next_offset is not None,
            "total_chars": total or offset + len(text)}
    if next_offset is not None:
        page["next_offset"] = next_offset
    return json.dumps(page)


def events(calls, final_text="{}"):
    """Builds a claude -p stream: each call is (tool name, input, result text)."""
    stream = []
    for n, (name, tool_input, text) in enumerate(calls):
        stream.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{n}", "name": f"mcp__claude_ai_Granola_MCP__{name}", "input": tool_input}]}})
        stream.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "content": [{"type": "text", "text": text}]}]}})
    stream.append({"type": "result", "result": final_text})
    return stream


class ParseTest(unittest.TestCase):
    def test_tool_results_pairs_use_and_result(self):
        results = ga.tool_results(events([("get_meetings", {"meeting_ids": [MID]}, "hello")]))
        self.assertEqual(results, [("get_meetings", {"meeting_ids": [MID]}, "hello")])

    def test_tool_results_skips_text_messages(self):
        stream = [{"type": "system", "message": "retrying"}, *events([("get_meetings", {}, "ok")])]
        self.assertEqual(ga.tool_results(stream), [("get_meetings", {}, "ok")])

    def test_title_entities_are_unescaped(self):
        text = MEETINGS_TEXT.replace('title="Purchase approvals - SOP"', 'title="DMS E^ &lt;&gt; Otobot &amp; Co"')
        self.assertEqual(ga.parse_meetings(text)[MID]["title"], "DMS E^ <> Otobot & Co")

    def test_list_ids(self):
        listed = ga.parse_listed(LIST_TEXT)
        self.assertEqual([m["id"] for m in listed], [MID, "00000000-0000-4000-8000-000000000002"])

    def test_parse_meeting_block(self):
        meeting = ga.parse_meetings(MEETINGS_TEXT)[MID]
        self.assertEqual(meeting["title"], "Purchase approvals - SOP")
        self.assertEqual(meeting["meeting_at"], "2026-10-21T12:30:00.000+05:30")
        self.assertEqual([p["name"] for p in meeting["attendees"]], ["Asha Rao", "Ben", "Chen Li"])
        self.assertEqual(meeting["attendees"][1]["email"], "ben@example.com")
        self.assertIn("R\\&D Budget", meeting["summary"])
        self.assertEqual(meeting["attachments"][0]["text"], "Sheet “RnD budget <19102026>” is open.")

    def test_transcript_pages_join_in_order(self):
        pages = [transcript_page(5, "world", total=10), transcript_page(0, "hello", next_offset=5, total=10)]
        self.assertEqual(ga.join_transcript(pages), ("helloworld", "complete"))

    def test_transcript_gap_is_incomplete(self):
        pages = [transcript_page(0, "hello", next_offset=5, total=20)]
        self.assertEqual(ga.join_transcript(pages)[1], "incomplete")

    def test_transcript_pending_and_none(self):
        pending = json.dumps({"id": MID, "transcript": None, "transcript_unavailable": {"reason": "transcript_pending"}})
        gone = json.dumps({"id": MID, "transcript": None, "transcript_unavailable": {"reason": "no_transcript"}})
        self.assertEqual(ga.join_transcript([pending])[1], "pending")
        self.assertEqual(ga.join_transcript([gone]), ("", "none"))

    def test_empty_transcript_with_reason_is_none(self):
        page = json.dumps({"id": MID, "transcript": "", "transcript_unavailable": {"reason": "no_transcript"}})
        self.assertEqual(ga.join_transcript([page]), ("", "none"))

    def test_metadata_tolerates_prose_around_json(self):
        text = 'Here you go:\n{"' + MID + '": {"summary": "x"}}\nDone.'
        self.assertEqual(ga.parse_metadata(text), {MID: {"summary": "x"}})
        self.assertEqual(ga.parse_metadata("no json"), {})

    def test_filename_is_safe(self):
        self.assertEqual(ga.note_name("2026-10-21T12:30:00.000+05:30", 'MR / SOP: "v2"?'),
                         "2026-10-21 1230 — MR - SOP- -v2--.md")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Meetings"
        self.state = Path(self.tmp.name) / "state"
        meta = {MID: {"summary": "Budget overrun; approval SOP agreed", "people": ["Dana Kim"],
                      "projects": ["Approvals 2.0"], "topics": ["Budget"], "tags": ["budget", "approvals"],
                      "decisions": ["Two-stage L1 approval"],
                      "action_items": [{"owner": "Dana Kim", "task": "Correct PO-2026-0042"}]}}
        self.stream = events([
            ("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
            ("get_meeting_transcript", {"meeting_id": MID}, transcript_page(0, "[0:00] Microphone: hi\n", total=22)),
        ], final_text=json.dumps(meta))

    def archive(self, stream):
        return ga.archive_batch([MID], self.root, self.state, NOW, run=lambda prompt, tools: stream)

    def test_writes_obsidian_note(self):
        self.assertEqual(self.archive(self.stream), [MID])
        note = (self.root / "2026" / "2026-10-21 1230 — Purchase approvals - SOP.md").read_text()
        self.assertTrue(note.startswith("---\ntype: meeting\n"))
        self.assertIn('attendees: ["[[Asha Rao]]", "[[Ben]]", "[[Chen Li]]"]', note)
        self.assertIn('projects: ["[[Approvals 2.0]]"]', note)
        self.assertIn("granola_id: " + MID, note)
        self.assertIn("- [ ] Dana Kim — Correct PO-2026-0042", note)
        self.assertIn("## Transcript\n\n[0:00] Microphone: hi", note)
        self.assertIn("Sheet “RnD budget <19102026>” is open.", note)
        index = (self.root / "Index.md").read_text()
        self.assertIn("[[2026-10-21 1230 — Purchase approvals - SOP]]", index)

    def test_meetings_sync_prompt_is_lean_and_complete(self):
        self.archive(self.stream)
        archive = json.loads((self.state / "granola_archived.json").read_text())
        self.assertEqual(archive[MID]["actions"], ["Dana Kim — Correct PO-2026-0042"])
        prompt = ga.meetings_sync_prompt([MID], archive, NOW, "Asha Rao")
        self.assertIn("MoM sync · 23 Oct", prompt)
        self.assertIn("Archived 1 · total 1", prompt)
        self.assertIn("• 12:30 Purchase approvals - SOP — 1 action", prompt)
        # Dana's action stays in the meeting note; only Asha's become his to-dos
        self.assertIn("To-Do items: none", prompt)
        archive[MID]["actions"].append("Asha Rao — Share approval SOP draft")
        prompt = ga.meetings_sync_prompt([MID], archive, NOW, "Asha Rao")
        self.assertIn("Share approval SOP draft | ref: granola/" + MID, prompt)

    def test_archived_once(self):
        self.archive(self.stream)
        self.assertEqual(self.archive(self.stream), [])
        self.assertEqual(ga.archived_ids(self.state), {MID})

    def test_pending_transcript_is_retried_later(self):
        pending = json.dumps({"id": MID, "transcript": None, "transcript_unavailable": {"reason": "transcript_pending"}})
        stream = events([("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
                         ("get_meeting_transcript", {"meeting_id": MID}, pending)])
        self.assertEqual(self.archive(stream), [])
        self.assertEqual(ga.archived_ids(self.state), set())

    def test_batched_transcript_call_is_read(self):
        batched = json.dumps({"content_advisory": "data", "meetings": [
            {"meeting_id": MID, "id": MID, "offset": 0, "truncated": False, "total_chars": 3, "transcript": "hey"}]})
        stream = events([("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
                         ("get_meeting_transcript", {"meeting_ids": [MID], "max_chars": 80000}, batched)])
        self.assertEqual(self.archive(stream), [MID])
        self.assertIn("## Transcript\n\nhey", next(self.root.glob("2026/*.md")).read_text())

    def test_oversized_result_is_read_from_saved_file(self):
        saved = Path(self.tmp.name) / "mcp-claude_ai_Granola_MCP-get_meeting_transcript-1.txt"
        saved.write_text(transcript_page(0, "long transcript", total=15))
        error = (f"Error: result (80,601 characters across 22 lines) exceeds maximum allowed tokens. "
                 f"Output has been saved to {saved}. Format: Plain text - For targeted searches use grep")
        stream = events([("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
                         ("get_meeting_transcript", {"meeting_id": MID}, error)])
        self.assertEqual(self.archive(stream), [MID])
        self.assertIn("long transcript", next(self.root.glob("2026/*.md")).read_text())

    def test_persisted_output_json_blocks_are_read(self):
        saved = Path(self.tmp.name) / "toolu_01.json"
        saved.write_text(json.dumps([{"type": "text", "text": transcript_page(0, "persisted transcript", total=20)}]))
        pointer = f"<persisted-output>\nOutput too large (80.9KB). Full output saved to: {saved}\n\nPreview (first 2KB):\n["
        stream = events([("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
                         ("get_meeting_transcript", {"meeting_id": MID}, pointer)])
        self.assertEqual(self.archive(stream), [MID])
        self.assertIn("persisted transcript", next(self.root.glob("2026/*.md")).read_text())

    def test_missing_metadata_still_archives(self):
        stream = events([("get_meetings", {"meeting_ids": [MID]}, MEETINGS_TEXT),
                         ("get_meeting_transcript", {"meeting_id": MID}, transcript_page(0, "hi", total=2))],
                        final_text="sorry")
        self.assertEqual(self.archive(stream), [MID])
        note = next(self.root.glob("2026/*.md")).read_text()
        self.assertIn("enriched: false", note)


if __name__ == "__main__":
    unittest.main()
