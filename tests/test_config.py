import tempfile
import unittest
from pathlib import Path

import config


class ConfigTest(unittest.TestCase):
    def test_reads_keys_and_ignores_comments_quotes_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.env"
            path.write_text('# my setup\nSLACK_USER_ID=U123\nexport OWNER_NAME="Asha Rao"\n\nMEETINGS_DIR=\'~/Drive/Meetings\'\n')
            self.assertEqual(config.load(path), {"SLACK_USER_ID": "U123", "OWNER_NAME": "Asha Rao",
                                                 "MEETINGS_DIR": "~/Drive/Meetings"})

    def test_missing_file_is_empty(self):
        self.assertEqual(config.load(Path("/nonexistent/config.env")), {})

    def test_require_names_the_missing_key(self):
        with self.assertRaises(SystemExit) as raised:
            config.require({}, "SLACK_USER_ID")
        self.assertIn("SLACK_USER_ID", str(raised.exception))
        self.assertIn("config.env", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
