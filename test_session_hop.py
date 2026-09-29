import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import re
import sqlite3

import session_hop
from session_hop import (Picker, Spinner, cells, connect, luminance, launch, main, parse_session, prompt_text, resolve, search,
                           split_keys, sync, with_default_command)


class SessionIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pi = self.root / "pi"
        self.claude = self.root / "claude"
        self.codex = self.root / "codex" / "sessions"
        self.pi.mkdir()
        self.claude.mkdir()
        self.codex.mkdir(parents=True)
        self.project = self.root / "project"
        self.project.mkdir()
        self.db = connect(self.root / "private" / "index.sqlite3")
        self.addCleanup(self.db.close)
        self.sources = {"pi": self.pi, "claude": self.claude, "codex": self.codex}

    def resumed(self, row):
        """The directory launch() changes to and the command it replaces this process with."""
        with patch("os.chdir") as chdir, patch("os.execvp") as execvp:
            launch(row)
        return str(chdir.call_args.args[0]), execvp.call_args.args[1]

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
        self.assertEqual(self.resumed(row), (str(self.project), ["pi", "--session", str(path)]))

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
        self.assertEqual(self.resumed(row)[1], ["claude", "--resume", "abc-123"])

    def test_rename_changes_title_not_note_and_title_alias_still_works(self):
        self.write_lines(self.pi / "2026_test-pi.jsonl", [
            {"type": "session", "id": "test-pi", "cwd": str(self.project)},
            {"type": "message", "message": {"role": "user", "content": "Fix billing"}},
        ])
        options = ["--db", str(self.root / "cli.sqlite3"), "--pi-dir", str(self.pi),
                   "--claude-dir", str(self.claude), "--codex-dir", str(self.codex)]
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


    def cli(self, *args, terminal=True):
        """Run the CLI; returns the exit code, printed text, and the sessions and query handed to the picker."""
        options = ["--db", str(self.root / "cli.sqlite3"), "--pi-dir", str(self.pi),
                   "--claude-dir", str(self.claude), "--codex-dir", str(self.codex)]
        with patch("builtins.print") as printed, patch("sys.stderr"), \
                patch.object(session_hop, "interactive", return_value=terminal), \
                patch.object(session_hop, "picker", return_value=None) as shown:
            code = main(options + list(args))
        out = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
        if not shown.called:
            return code, out, []
        rows, query, _, bookmarked = shown.call_args.args[1:5]
        return code, out, Picker(rows, query, bookmarked=bookmarked).visible

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
        code, _, shown = self.cli("billbee")
        self.assertEqual(code, 0)
        self.assertEqual([row["display_title"] for row in shown], ["Billbee invoices"])
        self.assertEqual(self.cli("billbee", terminal=False)[0], 1)  # No list output without a terminal.

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
        shown = self.cli("-b")[2]
        self.assertEqual([(row["sid"], row["display_note"]) for row in shown], [("01a0da86", "needs tests")])
        self.assertEqual(self.cli("unbookmark", "01a0")[0], 0)
        self.assertEqual(self.cli("-b")[2], [])

    def test_picker_filters_moves_scopes_bookmarks_and_opens(self):
        rows = [{"agent": "pi", "sid": s, "cwd": cwd, "project": cwd, "display_title": title,
                 "display_note": "", "tags": "", "bookmarked": 0, "updated": 0}
                for s, cwd, title in (("a", "/p", "Billbee orders"), ("b", "/p", "Billbee stock"),
                                      ("c", "/other", "Other"))]
        toggled = []
        picker = Picker(rows, "billbee", lambda row, value: toggled.append((row["sid"], value)), cwd="/p")
        self.assertEqual([r["sid"] for r in picker.visible], ["a", "b"])
        picker.handle("down")
        picker.handle("down")
        self.assertEqual(picker.selected["sid"], "b")
        picker.handle("bookmark")
        self.assertEqual(toggled, [("b", True)])
        rows[1]["bookmarked"] = 1  # What the bookmark callback does to the row.
        picker.handle("starred")
        self.assertEqual([r["sid"] for r in picker.visible], ["b"])  # Ctrl-S: bookmarked only.
        self.assertIn("★ bookmarked", picker.render(80, 9)[0][0])
        picker.handle("starred")
        self.assertEqual([r["sid"] for r in picker.visible], ["a", "b"])
        for key in " stock":
            picker.handle(key)
        self.assertEqual([r["sid"] for r in picker.visible], ["b"])
        self.assertEqual(picker.handle("enter"), "open")
        picker.handle("clear")
        self.assertEqual(len(picker.visible), 3)
        picker.handle("tab")
        self.assertEqual([r["sid"] for r in picker.visible], ["a", "b"])
        self.assertIn("cwd p", picker.render(80, 9)[0][0])
        picker.handle("tab")
        self.assertEqual(len(picker.visible), 3)
        self.assertEqual(picker.handle("esc"), "quit")


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

    def test_split_keys_handles_sequences_batches_and_text(self):
        self.assertEqual(split_keys("\x1b[B\x1b[Bab\r"), ["down", "down", "a", "b", "enter"])
        self.assertEqual(split_keys("\x1bOA\x1b"), ["up", "esc"])
        self.assertEqual(split_keys("\x02\x13\t"), ["bookmark", "starred", "tab"])
        self.assertEqual(split_keys("\x1b[1;5C\x01ž"), ["ž"])  # Unknown keys are ignored.

    def test_render_fits_height_and_width_with_theme_palette_only(self):
        rows = [{"agent": "claude", "sid": f"id{i}", "cwd": "/p", "project": "/p", "display_title": f"Task {i} 日本語",
                 "display_note": "note", "tags": "", "bookmarked": i == 0, "updated": 0} for i in range(20)]
        picker = Picker(rows, "task", scope="project p")
        for _ in range(12):
            picker.handle("down")
        lines, column = picker.render(60, 9)
        self.assertEqual(len(lines), 9)
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
        self.assertTrue(all(cells(line) <= 59 for line in plain))
        self.assertTrue(plain[0].startswith("❯ task") and plain[0].endswith("project p  all  20/20"))
        self.assertFalse(any("▌" in line for line in plain))
        self.assertEqual(plain[1].split(), ["agent", "project", "title", "age"])
        self.assertEqual(plain[1].index("agent"), cells("❯ "))  # Agent column lines up with the cursor.
        chosen = next(line for line in plain if "Task 12" in line)
        styled = lines[plain.index(chosen)]
        for text in ("claude", "p ", "Task", "1970"):  # Every column of the active row is bold.
            self.assertRegex(styled, r"\x1b\[0;1(;\d+)*m *" + re.escape(text))
        self.assertEqual(plain[1].index("title"), chosen.index("Task"))  # Column names line up with rows.
        self.assertIn("Task 12", "".join(plain))
        self.assertEqual(column, cells("❯ task"))
        codes = {code for line in lines for group in re.findall(r"\x1b\[([0-9;]*)m", line) for code in group.split(";")}
        self.assertTrue(codes <= {"", "0", "1", "2", "3", "4", "32", "33", "34", "35", "36", "100"}, codes)
        self.assertEqual(plain[-4], "")  # Breathing room between the list and the details.
        self.assertEqual(plain[-3], "  claude:id12  ·  /p  ·  1970")  # Agent, directory, age.
        self.assertEqual((plain[-2], plain[-1][:4]), ("  note", "  ↑↓"))
        self.assertTrue(re.sub(r"\x1b\[[0-9;]*m", "", Picker(rows).render(60, 9)[0][0]).endswith("all  20/20"))
        no_color = Picker(rows, "task", color=False).render(60, 9)[0]
        self.assertNotRegex("".join(no_color), r"\x1b\[[0-9;]*(3\d|100)")

    def test_luminance_reads_osc11_replies(self):
        self.assertGreater(luminance(b"\x1b]11;rgb:f8f8/f9f9/fafa\x07"), 0.9)
        self.assertLess(luminance(b"\x1b]11;rgb:0b0b/0e0e/1414\x1b\\"), 0.1)
        self.assertLess(luminance(b"\x1b]11;rgb:1f/24/30\x07"), 0.2)
        self.assertIsNone(luminance(b"\x1b[?62;22c"))

    def test_legacy_index_moves_once_to_the_new_name(self):
        legacy, current = self.root / "share" / "agent-sessions", self.root / "share" / "session-hop"
        legacy.mkdir(parents=True)
        (legacy / "index.sqlite3").write_text("old")
        with patch.object(session_hop, "LEGACY_DATA_DIR", legacy), patch.object(session_hop, "DATA_DIR", current):
            session_hop.migrate_legacy_data()
            self.assertEqual((current / "index.sqlite3").read_text(), "old")
            legacy.mkdir()  # A leftover old folder never overwrites the current index.
            session_hop.migrate_legacy_data()
            self.assertTrue(legacy.exists())
            self.assertEqual((current / "index.sqlite3").read_text(), "old")

    def codex_session(self, sid, *entries):
        meta = {"type": "session_meta", "payload": {"id": sid, "cwd": str(self.project), "originator": "codex_cli_rs"}}
        return self.write_lines(self.codex / "2026" / "09" / "28" / f"rollout-2026-09-28T10-00-00-{sid}.jsonl",
                                [meta, *entries])

    def test_codex_sessions_use_typed_text_thread_names_and_resume(self):
        injected = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>Always commit</INSTRUCTIONS>"}]}}
        typed = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [
            {"type": "text", "text": "add game cards [Image: IMG_1.jpg; ref=image_1] [Attached image \"a.png\" is saved at: /tmp/a.png]\n<t3_context version=\"1\">x</t3_context>"}]}}}
        answer = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage"}}}
        self.codex_session("01a0-new", injected, typed, answer)
        sync(self.db, self.sources)
        row = resolve(self.db, "codex:01a0-new")
        self.assertEqual((row["title"], row["description"]), ("add game cards", "add game cards"))
        self.assertEqual(self.resumed(row), (str(self.project), ["codex", "resume", "01a0-new"]))
        # Renaming a thread only appends to Codex's own index; the next scan still picks it up.
        (self.codex.parent / "session_index.jsonl").write_text(
            json.dumps({"id": "01a0-new", "thread_name": "Old name"}) + "\n"
            + json.dumps({"id": "01a0-new", "thread_name": "Game cards"}) + "\n")
        self.assertEqual(sync(self.db, self.sources), (0, 0))
        self.assertEqual(resolve(self.db, "01a0-new")["title"], "Game cards")

    def test_older_codex_formats_fall_back_or_are_skipped(self):
        context = {"type": "input_text", "text": "<environment_context><cwd>/p</cwd></environment_context>"}
        self.codex_session("0199-old", {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [context, {"type": "input_text", "text": "fix the checkout"}]}})
        self.write_lines(self.codex / "2025" / "07" / "rollout-2025-07-28-legacy.jsonl", [
            {"id": "legacy", "timestamp": "2025-07-28T08:08:29Z", "instructions": None},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "no cwd recorded"}]}])
        typed = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": "automated"}]}}}
        for sid, source in (("exec-run", "exec"), ("sub-agent", {"subagent": "review"})):
            path = self.codex_session(sid, typed)
            lines = path.read_text().splitlines()
            meta = json.loads(lines[0])
            meta["payload"]["source"] = source
            path.write_text("\n".join([json.dumps(meta), *lines[1:]]) + "\n")
        self.assertEqual(sync(self.db, self.sources), (1, 0))
        self.assertEqual([row["sid"] for row in search(self.db)], ["0199-old"])
        self.assertEqual(resolve(self.db, "codex:0199")["title"], "fix the checkout")
        # Skipped files aren't re-read until they change.
        with patch.object(session_hop, "parse_session", wraps=parse_session) as parsed:
            self.assertEqual(sync(self.db, self.sources), (0, 0))
        parsed.assert_not_called()

    def test_codex_thread_continued_in_newer_file_indexed_once(self):
        def typed(text):
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": text}]}}}
        first = self.codex_session("01a0-long", typed("review the pull request"))
        meta = json.loads(first.read_text().splitlines()[0])
        later = self.write_lines(first.with_name("rollout-2026-09-28T12-00-00-01a0-long_01a0-page.jsonl"),
                                 [meta, typed("now fix the first finding")])
        os.utime(first, ns=(1_000_000_000, 1_000_000_000))
        os.utime(later, ns=(2_000_000_000, 2_000_000_000))
        self.assertEqual(sync(self.db, self.sources)[1], 0)
        row = resolve(self.db, "01a0-long")
        self.assertEqual((row["source_path"], row["title"]), (str(later), "now fix the first finding"))
        with patch.object(session_hop, "parse_session", wraps=parse_session) as parsed:
            self.assertEqual(sync(self.db, self.sources), (0, 0))
        parsed.assert_not_called()
        # Losing the newest file falls back to the older one and keeps the note.
        self.db.execute("UPDATE sessions SET note='keep'")
        self.db.commit()
        later.unlink()
        self.assertEqual(sync(self.db, self.sources), (1, 0))
        row = resolve(self.db, "01a0-long")
        self.assertEqual((row["source_path"], row["title"], row["note"]), (str(first), "review the pull request", "keep"))
        first.unlink()
        self.assertEqual(sync(self.db, self.sources), (0, 1))
        self.assertEqual(search(self.db), [])

    def test_sync_reports_progress_over_changed_files_only(self):
        for sid in ("a", "b", "c"):
            self.pi_session(sid, self.project, f"Task {sid}")
        calls = []
        sync(self.db, self.sources, lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])
        calls.clear()
        self.pi_session("d", self.project, "Task d")
        sync(self.db, self.sources, lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(1, 1)])

    def test_spinner_silent_when_fast_and_cleans_up_when_slow(self):
        fast = io.StringIO()
        with Spinner(fast, delay=0.2) as spinner:
            spinner(1, 2)
        self.assertEqual(fast.getvalue(), "")
        slow = io.StringIO()
        with Spinner(slow, delay=0, color=False) as spinner:
            time.sleep(0.03)
            spinner(312, 1693)
            time.sleep(0.2)  # Frames redraw every 80ms.
        self.assertIn("Checking for changed sessions", slow.getvalue())
        self.assertIn("Reading sessions 312/1693", slow.getvalue())
        self.assertTrue(slow.getvalue().endswith("\r\x1b[2K"))  # The line is erased before the picker draws.


if __name__ == "__main__":
    unittest.main()
