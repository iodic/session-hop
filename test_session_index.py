import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_index import connect, launch, parse_session, resolve, search, sync


class SessionIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pi = self.root / "pi"
        self.claude = self.root / "claude"
        self.pi.mkdir()
        self.claude.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.db = connect(self.root / "private" / "index.sqlite3")
        self.addCleanup(self.db.close)
        self.sources = {"pi": self.pi, "claude": self.claude}

    def write_lines(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        return path

    def test_pi_backfill_name_and_prompt_no_tool_output(self):
        path = self.write_lines(self.pi / "2026_test-pi.jsonl", [
            {"type": "session", "id": "test-pi", "cwd": str(self.project)},
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Fix the billing bug"}]}},
            {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "private secret"}]}},
            {"type": "session_info", "name": "Billing repairs"},
        ])
        self.assertEqual(sync(self.db, self.sources), (1, 0))
        row = resolve(self.db, "pi:test-pi")
        self.assertEqual(row["title"], "Billing repairs")
        self.assertEqual(row["description"], "Fix the billing bug")
        self.assertNotIn("private secret", str(dict(row)))
        self.assertEqual(sync(self.db, self.sources), (0, 0))
        with patch("builtins.print") as printed:
            launch(row, dry_run=True)
        self.assertIn(str(path), printed.call_args.args[0])
        self.assertIn("pi --session", printed.call_args.args[0])

    def test_claude_titles_and_manual_edits_survive_updates(self):
        path = self.write_lines(self.claude / "project" / "abc-123.jsonl", [
            {"type": "user", "sessionId": "abc-123", "cwd": str(self.project), "message": {"content": "Investigate tabs"}},
            {"type": "user", "sessionId": "abc-123", "cwd": str(self.project), "message": {"content": [{"type": "tool_result", "content": "secret"}]}},
            {"type": "ai-title", "aiTitle": "Tab search", "sessionId": "abc-123"},
            {"type": "custom-title", "customTitle": "Find old tabs", "sessionId": "abc-123"},
        ])
        sync(self.db, self.sources)
        row = resolve(self.db, "claude:abc")
        self.assertEqual(row["title"], "Find old tabs")
        self.assertEqual(row["description"], "Investigate tabs")
        self.db.execute("UPDATE sessions SET note=?, custom_title=? WHERE agent=? AND sid=?",
                        ("Priority session", "My title", "claude", "abc-123"))
        self.db.commit()
        with path.open("a") as file:
            file.write(json.dumps({"type": "ai-title", "aiTitle": "New title", "sessionId": "abc-123"}) + "\n")
        self.assertEqual(sync(self.db, self.sources), (1, 0))
        row = search(self.db, "priority my title")[0]
        self.assertEqual(row["display_title"], "My title")
        self.assertEqual(row["display_note"], "Priority session")
        with patch("builtins.print") as printed:
            launch(row, dry_run=True)
        self.assertIn("claude --resume abc-123", printed.call_args.args[0])

    def test_missing_file_removed_but_missing_root_preserved(self):
        path = self.write_lines(self.pi / "2026_test-pi.jsonl", [
            {"type": "session", "id": "test-pi", "cwd": str(self.project)},
            {"type": "session_info", "name": "Archived work"},
        ])
        sync(self.db, self.sources)
        sync(self.db, {"pi": self.root / "unmounted"})
        self.assertEqual(len(search(self.db)), 1)
        path.unlink()
        self.assertEqual(sync(self.db, self.sources), (0, 1))
        self.assertEqual(search(self.db), [])

    def test_ignore_sidechain_and_incomplete_json(self):
        path = self.write_lines(self.claude / "p" / "abc.jsonl", [
            {"type": "user", "isSidechain": True, "sessionId": "abc", "cwd": str(self.project), "message": {"content": "ignore me"}},
            {"type": "user", "sessionId": "abc", "cwd": str(self.project), "message": {"content": [{"type": "text", "text": "Visible request"}]}},
        ])
        with path.open("a") as file:
            file.write('{"unfinished":')
        self.assertEqual(parse_session("claude", path)["title"], "Visible request")
        self.assertEqual(sync(self.db, self.sources), (1, 0))
        self.assertEqual(search(self.db, "ignore me"), [])


if __name__ == "__main__":
    unittest.main()
