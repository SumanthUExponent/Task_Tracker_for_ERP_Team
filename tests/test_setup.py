import plistlib
import tempfile
import unittest
from pathlib import Path

import setup


class RenderTest(unittest.TestCase):
    def test_fills_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            templates, out = Path(tmp, "templates"), Path(tmp, "out")
            templates.mkdir()
            (templates / "morning.md").write_text("You write {{OWNER}}'s brief. Run {{APP_DIR}}/cc_digest.py")
            written = setup.render(templates, out, {"OWNER": "Asha", "APP_DIR": "/Users/asha/personal-assistant"})
            self.assertEqual([p.name for p in written], ["morning.md"])
            self.assertEqual((out / "morning.md").read_text(),
                             "You write Asha's brief. Run /Users/asha/personal-assistant/cc_digest.py")

    def test_unknown_placeholder_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp, "templates")
            templates.mkdir()
            (templates / "x.md").write_text("{{TYPO}}")
            with self.assertRaises(SystemExit) as raised:
                setup.render(templates, Path(tmp, "out"), {"OWNER": "Asha"})
            self.assertIn("TYPO", str(raised.exception))

    def test_repo_templates_only_use_known_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup.render(setup.TEMPLATES, Path(tmp), {"OWNER": "Asha", "APP_DIR": "/x"})


class LaunchdTest(unittest.TestCase):
    def test_hourly_job(self):
        job = plistlib.loads(setup.job_plist("hourly", "/usr/bin/python3", "/app"))
        self.assertEqual(job["Label"], "com.personal-assistant.hourly")
        self.assertEqual(job["ProgramArguments"], ["/usr/bin/python3", "/app/actions.py", "tick"])
        self.assertEqual(job["StartInterval"], 3600)
        self.assertIn("/Library/Preferences/SystemConfiguration", job["WatchPaths"])
        self.assertEqual(job["StandardOutPath"], "/app/state/hourly.log")

    def test_nightly_granola_job(self):
        job = plistlib.loads(setup.job_plist("granola-archive", "/usr/bin/python3", "/app"))
        self.assertEqual(job["ProgramArguments"], ["/usr/bin/python3", "/app/granola_archive.py"])
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 21, "Minute": 7})
        self.assertIn("/opt/homebrew/bin", job["EnvironmentVariables"]["PATH"])


if __name__ == "__main__":
    unittest.main()
