import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import curses
import sqlite3

import session_index
from session_index import (Picker, connect, launch, main, parse_session, prompt_text, resolve, search,
                           sync, with_default_command)


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

    def test_rename_changes_title_not_note_and_title_alias_still_works(self):
        self.write_lines(self.pi / "2026_test-pi.jsonl", [
            {"type": "session", "id": "test-pi", "cwd": str(self.project)},
            {"type": "message", "message": {"role": "user", "content": "Fix billing"}},
        ])
        options = ["--db", str(self.root / "cli.sqlite3"), "--pi-dir", str(self.pi),
                   "--claude-dir", str(self.claude)]
        with patch("builtins.print"):
            self.assertEqual(main(options + ["rename", "pi:test-pi", "Better", "title"]), 0)
            self.assertEqual(main(options + ["note", "pi:test-pi", "Next", "step"]), 0)
        with connect(self.root / "cli.sqlite3") as conn:
            row = search(conn, "better next")[0]
            self.assertEqual(row["display_title"], "Better title")
            self.assertEqual(row["display_note"], "Next step")
        with patch("builtins.print"):
            self.assertEqual(main(options + ["title", "pi:test-pi", "Alias", "works"]), 0)
        with connect(self.root / "cli.sqlite3") as conn:
            self.assertEqual(search(conn, "alias works")[0]["display_title"], "Alias works")

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


    def cli(self, *args):
        options = ["--db", str(self.root / "cli.sqlite3"), "--pi-dir", str(self.pi),
                   "--claude-dir", str(self.claude)]
        with patch("builtins.print") as printed, patch.object(session_index, "interactive", return_value=False):
            code = main(options + list(args))
        return code, "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)

    def pi_session(self, sid, cwd, text):
        self.write_lines(self.pi / f"2026_{sid}.jsonl", [
            {"type": "session", "id": sid, "cwd": str(cwd)},
            {"type": "message", "message": {"role": "user", "content": text}},
        ])

    def test_bare_words_search_and_commands_still_route(self):
        self.assertEqual(with_default_command(["billbee"]), ["pick", "billbee"])
        self.assertEqual(with_default_command([]), ["pick"])
        self.assertEqual(with_default_command(["--db", "x", "-b"]), ["--db", "x", "pick", "-b"])
        self.assertEqual(with_default_command(["--db=x", "sync"]), ["--db=x", "sync"])
        self.assertEqual(with_default_command(["--", "sync"]), ["pick", "--", "sync"])
        self.pi_session("one", self.project, "Billbee invoices")
        self.pi_session("two", self.project, "Unrelated work")
        code, out = self.cli("billbee")
        self.assertEqual(code, 0)
        self.assertIn("Billbee invoices", out)
        self.assertNotIn("Unrelated", out)

    def test_project_root_recorded_and_filterable(self):
        repo = self.root / "billbee-api"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        self.pi_session("in-repo", repo / "src", "Sync orders")
        self.pi_session("elsewhere", self.project, "Sync orders too")
        sync(self.db, self.sources)
        self.assertEqual(resolve(self.db, "in-repo")["project"], str(repo))
        self.assertEqual([r["sid"] for r in search(self.db, "sync", project="billbee")], ["in-repo"])
        with patch("os.getcwd", return_value=str(repo / "src")):
            self.assertEqual([r["sid"] for r in search(self.db, project=".")], ["in-repo"])
        self.assertEqual([r["sid"] for r in search(self.db, project=str(self.project))], ["elsewhere"])

    def test_old_index_gains_columns_and_backfills_project(self):
        path = self.root / "old.sqlite3"
        with sqlite3.connect(path) as old:
            old.execute("""CREATE TABLE sessions (agent TEXT NOT NULL, sid TEXT NOT NULL, cwd TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, source_path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, updated REAL NOT NULL,
                custom_title TEXT, note TEXT, tags TEXT NOT NULL DEFAULT '', PRIMARY KEY(agent, sid))""")
            old.execute("INSERT INTO sessions VALUES ('pi','x',?,'t','d','/gone',0,0,0,NULL,NULL,'')",
                        (str(self.project),))
        conn = connect(path)
        self.addCleanup(conn.close)
        sync(conn, {})
        row = resolve(conn, "x")
        self.assertEqual((row["project"], row["bookmarked"]), (str(self.project), 0))

    def test_bookmark_by_short_prefix_with_note_and_filter(self):
        self.pi_session("01a0da86", self.project, "Half-done migration")
        self.pi_session("99ffee00", self.project, "Finished work")
        self.assertEqual(self.cli("bm", "01a0", "needs", "tests")[0], 0)
        code, out = self.cli("-b")
        self.assertIn("★ pi:01a0da86", out)
        self.assertIn("needs tests", out)
        self.assertNotIn("Finished", out)
        self.assertEqual(self.cli("unbookmark", "01a0")[0], 0)
        self.assertIn("No sessions found.", self.cli("-b")[1])

    def test_picker_filters_moves_bookmarks_and_opens(self):
        rows = [{"agent": "pi", "sid": s, "cwd": "/p", "project": "/p", "display_title": t,
                 "display_note": "", "tags": "", "bookmarked": 0, "updated": 0}
                for s, t in (("a", "Billbee orders"), ("b", "Billbee stock"), ("c", "Other"))]
        toggled = []
        picker = Picker(rows, "billbee", lambda row, value: toggled.append((row["sid"], value)))
        self.assertEqual([r["sid"] for r in picker.visible], ["a", "b"])
        picker.handle(curses.KEY_DOWN)
        picker.handle(curses.KEY_DOWN)
        self.assertEqual(picker.selected["sid"], "b")
        picker.handle("\t")
        self.assertEqual(toggled, [("b", True)])
        for key in " stock":
            picker.handle(key)
        self.assertEqual([r["sid"] for r in picker.visible], ["b"])
        self.assertEqual(picker.handle("\n"), "open")
        picker.handle("\x15")
        self.assertEqual(len(picker.visible), 3)
        self.assertEqual(picker.handle("\x1b"), "quit")


    def test_prompt_text_strips_agent_wrappers(self):
        skill = '<skill name="fizzy" location="/x/SKILL.md">\n' + "Long skill body. " * 50 + "\n</skill>\n\ncheck card 5902"
        self.assertEqual(prompt_text(skill).split(), ["/fizzy", "check", "card", "5902"])
        self.assertEqual(prompt_text("<command-name>/clear</command-name> <command-message>clear</command-message>"
                                     " <command-args></command-args>"), "")
        self.assertEqual(prompt_text("<command-message>fizzy</command-message> <command-name>/fizzy</command-name>"
                                     " <command-args>estimate card 5973</command-args>"), "/fizzy estimate card 5973")
        for noise in ("<local-command-stdout>Set model</local-command-stdout>", "[Request interrupted by user]",
                      "<task-notification><summary>done</summary></task-notification>",
                      "<bash-input> ls</bash-input>", "[Extension issues]\n  router.ts failed"):
            self.assertEqual(prompt_text(noise).strip(), "")
        self.assertEqual(prompt_text("[Image #1]can you read this").strip(), "can you read this")

    def test_bare_command_skipped_for_title_and_old_rows_reparsed(self):
        self.write_lines(self.claude / "p" / "cleared.jsonl", [
            {"type": "user", "sessionId": "cleared", "cwd": str(self.project),
             "message": {"content": "<command-name>/clear</command-name><command-args></command-args>"}},
            {"type": "user", "sessionId": "cleared", "cwd": str(self.project), "message": {"content": "Real request"}},
        ])
        sync(self.db, self.sources)
        self.assertEqual(resolve(self.db, "cleared")["title"], "Real request")
        self.db.execute("UPDATE sessions SET title='<command-name>/clear', note='mine', bookmarked=1")
        self.db.execute("PRAGMA user_version=0")
        self.db.commit()
        conn = connect(self.root / "private" / "index.sqlite3")
        self.addCleanup(conn.close)
        self.assertEqual(sync(conn, self.sources), (1, 0))
        row = resolve(conn, "cleared")
        self.assertEqual((row["title"], row["note"], row["bookmarked"]), ("Real request", "mine", 1))

    def test_bare_command_title_only_when_answered_and_empty_sessions_dropped(self):
        bare = {"type": "user", "cwd": str(self.project),
                "message": {"content": "<command-name>/qa</command-name><command-args></command-args>"}}
        self.write_lines(self.claude / "p" / "qa.jsonl", [
            {**bare, "sessionId": "qa"}, {"type": "assistant", "sessionId": "qa", "cwd": str(self.project)}])
        empty = self.write_lines(self.claude / "p" / "empty.jsonl", [{**bare, "sessionId": "empty"}])
        kept = self.write_lines(self.claude / "p" / "kept.jsonl", [{**bare, "sessionId": "kept"}])
        for path, sid in ((empty, "empty"), (kept, "kept")):  # Rows left behind by an older parser.
            self.db.execute("INSERT INTO sessions(agent, sid, cwd, title, description, source_path, mtime_ns, size,"
                            " updated) VALUES ('claude', ?, ?, 'junk', '', ?, 0, 0, 0)", (sid, str(self.project), str(path)))
        self.db.execute("UPDATE sessions SET bookmarked=1 WHERE sid='kept'")
        self.db.commit()
        self.assertEqual(sync(self.db, self.sources), (1, 1))
        self.assertEqual(resolve(self.db, "qa")["title"], "/qa")
        self.assertEqual(sorted(r["sid"] for r in search(self.db)), ["kept", "qa"])


if __name__ == "__main__":
    unittest.main()
