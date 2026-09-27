"""Local index of resumable Pi and Claude Code conversations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
from datetime import datetime

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-sessions"
DEFAULT_DB = DATA_DIR / "index.sqlite3"
DEFAULT_PI = Path.home() / ".pi/agent/sessions"
DEFAULT_CLAUDE = Path.home() / ".claude/projects"


def clean(text: str, limit: int) -> str:
    # Single-line metadata only. Never copy a transcript into the index.
    return re.sub(r"\s+", " ", text).strip()[:limit]


def user_text(content: object) -> str:
    if isinstance(content, str):
        return clean(content, 280)
    if isinstance(content, list):
        return clean(" ".join(block.get("text", "") for block in content
                              if isinstance(block, dict) and block.get("type") == "text"
                              and isinstance(block.get("text"), str)), 280)
    return ""


def parse_session(agent: str, path: Path) -> dict | None:
    sid = ""
    cwd = ""
    first = ""
    last = ""
    name = ""
    ai_title = ""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # An agent may be in the middle of appending a line.
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if agent == "pi":
                if kind == "session":
                    sid = entry.get("id", "")
                    cwd = entry.get("cwd", "")
                elif kind == "session_info":
                    name = entry.get("name") or ""
                elif kind == "message":
                    message = entry.get("message")
                    if isinstance(message, dict) and message.get("role") == "user":
                        text = user_text(message.get("content"))
                        if text:
                            first = first or text
                            last = text
            else:
                if kind in ("user", "assistant", "ai-title", "custom-title", "agent-name"):
                    sid = entry.get("sessionId") or sid
                    cwd = entry.get("cwd") or cwd
                if kind == "custom-title":
                    name = entry.get("customTitle") or ""
                elif kind == "ai-title":
                    ai_title = entry.get("aiTitle") or ""
                elif kind == "user" and not entry.get("isSidechain") and not entry.get("isMeta"):
                    message = entry.get("message")
                    if isinstance(message, dict):
                        text = user_text(message.get("content"))
                        if text:
                            first = first or text
                            last = text
    if agent == "claude":
        sid = sid or path.stem
    if not isinstance(sid, str) or not isinstance(cwd, str) or not cwd or not (first or name or ai_title):
        return None
    title = clean(name or ai_title or first, 100)
    # This is an excerpt, not an AI-generated summary. Manual notes override it.
    description = first if name or ai_title else (last if last != first else first)
    return {"agent": agent, "sid": sid, "cwd": cwd, "title": title,
            "description": clean(description, 280), "path": str(path)}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch(mode=0o600)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT NOT NULL, sid TEXT NOT NULL, cwd TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, source_path TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
            updated REAL NOT NULL, custom_title TEXT, note TEXT, tags TEXT NOT NULL DEFAULT '',
            project TEXT, bookmarked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(agent, sid)
        );
        CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated DESC);
        CREATE INDEX IF NOT EXISTS sessions_path ON sessions(source_path);
    """)
    # Indexes created before project and bookmark support gain the columns in place.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "project" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT")
    if "bookmarked" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN bookmarked INTEGER NOT NULL DEFAULT 0")
    return conn


def project_root(cwd: str) -> str:
    # The nearest Git checkout, so sessions started in subdirectories group together.
    # Home is never a project root, even when it holds a dotfiles repository.
    home = Path.home()
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if candidate == home:
            break
        if (candidate / ".git").exists():
            return str(candidate)
    return cwd


def sync(conn: sqlite3.Connection, sources: dict[str, Path]) -> tuple[int, int]:
    changed = 0
    removed = 0
    roots: dict[str, str] = {}
    for agent, root in sources.items():
        if not root.is_dir():
            continue  # A temporarily unavailable disk must not wipe the index.
        seen: set[str] = set()
        for path in root.rglob("*.jsonl"):
            if agent == "claude" and "subagents" in path.parts:
                continue
            try:
                stat = path.stat()
                old = conn.execute("SELECT mtime_ns, size FROM sessions WHERE agent=? AND source_path=?",
                                   (agent, str(path))).fetchone()
                seen.add(str(path))
                if old and old["mtime_ns"] == stat.st_mtime_ns and old["size"] == stat.st_size:
                    continue
                info = parse_session(agent, path)
                if not info:
                    continue
                if info["cwd"] not in roots:
                    roots[info["cwd"]] = project_root(info["cwd"])
                conn.execute("""
                    INSERT INTO sessions(agent, sid, cwd, title, description, source_path, mtime_ns, size, updated, project)
                    VALUES (:agent, :sid, :cwd, :title, :description, :path, :mtime_ns, :size, :updated, :project)
                    ON CONFLICT(agent, sid) DO UPDATE SET
                        cwd=excluded.cwd, title=excluded.title, description=excluded.description,
                        source_path=excluded.source_path, mtime_ns=excluded.mtime_ns,
                        size=excluded.size, updated=excluded.updated, project=excluded.project
                """, {**info, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "updated": stat.st_mtime,
                      "project": roots[info["cwd"]]})
                changed += 1
            except (OSError, ValueError, TypeError):
                continue  # A deleted or malformed file does not stop the rest of the scan.
        for row in conn.execute("SELECT sid, source_path FROM sessions WHERE agent=?", (agent,)).fetchall():
            if row["source_path"] not in seen:
                conn.execute("DELETE FROM sessions WHERE agent=? AND sid=?", (agent, row["sid"]))
                removed += 1
    # Rows indexed before project support are unchanged on disk, so fill them in here.
    for row in conn.execute("SELECT agent, sid, cwd FROM sessions WHERE project IS NULL").fetchall():
        if row["cwd"] not in roots:
            roots[row["cwd"]] = project_root(row["cwd"])
        conn.execute("UPDATE sessions SET project=? WHERE agent=? AND sid=?",
                     (roots[row["cwd"]], row["agent"], row["sid"]))
    conn.commit()
    return changed, removed


def all_sessions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT *, COALESCE(custom_title, title) AS display_title, "
        "COALESCE(note, description) AS display_note FROM sessions ORDER BY updated DESC")]


def matches(row: dict, terms: list[str]) -> bool:
    haystack = " ".join(str(row[k]) for k in ("agent", "sid", "cwd", "project", "display_title",
                                               "display_note", "tags")).casefold()
    return all(term in haystack for term in terms)


def in_project(value: str):
    # A path (".", "~/x", "a/b") selects that checkout; a bare word matches project names.
    if value.startswith((".", "~")) or os.sep in value:
        path = os.path.abspath(os.path.expanduser(value))
        # Agents may record either side of a symlink such as /var -> /private/var.
        roots = {project_root(path), project_root(os.path.realpath(path))}
        prefixes = tuple(root.rstrip(os.sep) + os.sep for root in roots)
        return lambda row: row["cwd"] in roots or row["cwd"].startswith(prefixes)
    name = value.casefold()
    return lambda row: name in Path(row["project"] or row["cwd"]).name.casefold()


def search(conn: sqlite3.Connection, query: str = "", limit: int | None = None, *,
           project: str | None = None, bookmarked: bool = False) -> list[dict]:
    terms = query.casefold().split()
    keep = in_project(project) if project else None
    found = []
    for row in all_sessions(conn):
        if (bookmarked and not row["bookmarked"]) or (keep and not keep(row)) or not matches(row, terms):
            continue
        found.append(row)
        if limit and len(found) >= limit:
            break
    return found


def set_bookmark(conn: sqlite3.Connection, row, value: bool) -> None:
    conn.execute("UPDATE sessions SET bookmarked=? WHERE agent=? AND sid=?", (int(value), row["agent"], row["sid"]))
    conn.commit()
    if isinstance(row, dict):
        row["bookmarked"] = int(value)


def resolve(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    agent = None
    if ":" in key:
        agent, key = key.split(":", 1)
        if agent not in ("pi", "claude"):
            raise ValueError("Use pi:<id> or claude:<id>.")
    if not key:
        raise ValueError("Provide a session ID prefix.")
    rows = [row for row in conn.execute("SELECT * FROM sessions" + (" WHERE agent=?" if agent else ""),
                                       (agent,) if agent else ()) if row["sid"].startswith(key)]
    if len(rows) != 1:
        raise ValueError("No matching session." if not rows else "Ambiguous ID; use agent:<longer-id>.")
    return rows[0]


def resume_argv(row: sqlite3.Row) -> list[str]:
    return ["pi", "--session", row["source_path"]] if row["agent"] == "pi" else ["claude", "--resume", row["sid"]]


def format_row(row) -> str:
    date = datetime.fromtimestamp(row["updated"]).strftime("%Y-%m-%d")
    project = Path(row["project"] or row["cwd"]).name or row["cwd"]
    mark = "★" if row["bookmarked"] else " "
    return f"{mark} {row['agent'] + ':' + row['sid'][:8]:<15}  {date}  {project[:20]:<20}  {row['display_title']}"


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(format_row(row))
        print(f"     {row['display_note']}")
    if not rows:
        print("No sessions found.")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class Picker:
    """Filter-as-you-type session list. Kept free of curses calls except in draw() for testing."""

    HELP = "↑↓ move  enter open  tab bookmark  esc quit"

    def __init__(self, rows: list[dict], query: str = "", on_bookmark=None):
        self.rows = rows
        self.query = query
        self.on_bookmark = on_bookmark
        self.index = 0
        self.top = 0
        self.page = 10
        self.refilter()

    def refilter(self) -> None:
        terms = self.query.casefold().split()
        self.visible = [row for row in self.rows if matches(row, terms)]
        self.index = 0

    @property
    def selected(self) -> dict | None:
        return self.visible[self.index] if self.visible else None

    def move(self, step: int) -> None:
        if self.visible:
            self.index = max(0, min(len(self.visible) - 1, self.index + step))

    def handle(self, key) -> str | None:
        import curses
        if key in ("\n", "\r", curses.KEY_ENTER):
            return "open" if self.visible else None
        if key == "\x1b":
            return "quit"
        if key in (curses.KEY_UP, "\x10"):  # Ctrl-P
            self.move(-1)
        elif key in (curses.KEY_DOWN, "\x0e"):  # Ctrl-N
            self.move(1)
        elif key == curses.KEY_PPAGE:
            self.move(-self.page)
        elif key == curses.KEY_NPAGE:
            self.move(self.page)
        elif key == "\t":
            row = self.selected
            if row and self.on_bookmark:
                self.on_bookmark(row, not row["bookmarked"])
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.query = self.query[:-1]
            self.refilter()
        elif key == "\x15":  # Ctrl-U
            self.query = ""
            self.refilter()
        elif isinstance(key, str) and key.isprintable():
            self.query += key
            self.refilter()
        return None

    def draw(self, screen) -> None:
        import curses
        screen.erase()
        height, width = screen.getmaxyx()

        def put(y: int, x: int, text: str, attr: int = 0) -> None:
            if 0 <= y < height and x < width - 1:
                try:
                    screen.addnstr(y, x, text, width - x - 1, attr)
                except curses.error:
                    pass  # Wide characters can overrun the last column.

        self.page = max(1, height - 5)
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + self.page:
            self.top = self.index - self.page + 1
        count = f"{len(self.visible)}/{len(self.rows)}"
        put(0, max(0, width - len(count) - 1), count, curses.A_DIM)
        for line, row in enumerate(self.visible[self.top:self.top + self.page], 1):
            chosen = self.top + line - 1 == self.index
            put(line, 0, ("▸" if chosen else " ") + format_row(row), curses.A_REVERSE if chosen else 0)
        if not self.visible:
            put(1, 2, "No matching sessions.", curses.A_DIM)
        row = self.selected
        if row:
            put(height - 3, 2, row["cwd"], curses.A_DIM)
            put(height - 2, 2, row["display_note"])
        put(height - 1, 2, self.HELP, curses.A_DIM)
        put(0, 0, f"> {self.query}", curses.A_BOLD)
        try:
            screen.move(0, min(width - 1, 2 + len(self.query)))
        except curses.error:
            pass


def picker(conn: sqlite3.Connection, rows: list[dict], query: str = "") -> dict | None:
    import curses
    os.environ.setdefault("ESCDELAY", "25")  # Esc should quit without the default one-second pause.
    state = Picker(rows, query, lambda row, value: set_bookmark(conn, row, value))

    def run(screen):
        screen.keypad(True)
        while True:
            state.draw(screen)
            action = state.handle(screen.get_wch())
            if action == "quit":
                return None
            if action == "open":
                return state.selected

    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return None


def launch(row: sqlite3.Row, dry_run: bool = False) -> None:
    cwd = Path(row["cwd"])
    if not cwd.is_dir():
        raise ValueError(f"Project directory no longer exists: {cwd}")
    if not Path(row["source_path"]).is_file():
        raise ValueError("Session file no longer exists; run 'agent-sessions sync'.")
    argv = resume_argv(row)
    if dry_run:
        print(f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}")
        return
    os.chdir(cwd)
    os.execvp(argv[0], argv)


COMMANDS = {"sync", "find", "pick", "open", "rename", "title", "note", "tag", "bookmark", "bm", "unbookmark", "unbm"}
GLOBAL_OPTIONS = {"--db", "--pi-dir", "--claude-dir"}


def with_default_command(argv: list[str]) -> list[str]:
    # "agent-sessions billbee" means "agent-sessions pick billbee".
    i = 0
    while i < len(argv):
        if argv[i] in GLOBAL_OPTIONS:
            i += 2
        elif argv[i].split("=", 1)[0] in GLOBAL_OPTIONS:
            i += 1
        else:
            break
    if i < len(argv) and (argv[i] in COMMANDS or argv[i] in ("-h", "--help")):
        return argv
    return argv[:i] + ["pick"] + argv[i:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find and resume Pi and Claude Code sessions",
        usage="agent-sessions [words ...] [-p PROJECT] [-b]\n       agent-sessions <command> ...",
        epilog="Without a command, words open the interactive picker: agent-sessions billbee",
        allow_abbrev=False)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite index path")
    parser.add_argument("--pi-dir", type=Path, default=DEFAULT_PI, help="Pi session directory")
    parser.add_argument("--claude-dir", type=Path, default=DEFAULT_CLAUDE, help="Claude projects directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Index new and changed sessions")
    for verb, help_text, limit in (("pick", "Choose a session and resume it (default)", None),
                                   ("find", "Print matching sessions, newest first", 30)):
        cmd = sub.add_parser(verb, help=help_text)
        cmd.add_argument("query", nargs="*", help="Words in title, note, path, tags, or ID")
        cmd.add_argument("-p", "--project", help="Project name, or a path such as '.' for the current checkout")
        cmd.add_argument("-b", "--bookmarked", action="store_true", help="Only bookmarked sessions")
        cmd.add_argument("--limit", type=int, default=limit)
    opening = sub.add_parser("open", help="Resume a session by ID prefix")
    opening.add_argument("id")
    opening.add_argument("--dry-run", action="store_true", help="Print the shell command instead")
    for verb, help_text in (("rename", "Set a session title"),
                            ("note", "Set a session description"),
                            ("tag", "Add a searchable tag")):
        cmd = sub.add_parser(verb, aliases=["title"] if verb == "rename" else [], help=help_text)
        cmd.add_argument("id")
        cmd.add_argument("text", nargs="+", help="New text")
    mark = sub.add_parser("bookmark", aliases=["bm"], help="Bookmark a session, optionally with a note")
    mark.add_argument("id")
    mark.add_argument("text", nargs="*", help="Optional note, e.g. what still needs doing")
    unmark = sub.add_parser("unbookmark", aliases=["unbm"], help="Remove a bookmark")
    unmark.add_argument("id")
    args = parser.parse_args(with_default_command(sys.argv[1:] if argv is None else list(argv)))
    if args.command in ("find", "pick") and args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    try:
        conn = connect(args.db)
        try:
            sources = {"pi": args.pi_dir, "claude": args.claude_dir}
            if args.command == "sync":
                updated, removed = sync(conn, sources)
                print(f"Indexed {updated} changed sessions; removed {removed} missing sessions. {conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]} total.")
            elif args.command in ("find", "pick"):
                sync(conn, sources)
                query = " ".join(args.query)
                if args.command == "find" or not interactive():
                    print_rows(search(conn, query, args.limit or 30, project=args.project,
                                      bookmarked=args.bookmarked))
                else:
                    # The picker filters as you type, so it starts from every candidate.
                    rows = search(conn, "", args.limit, project=args.project, bookmarked=args.bookmarked)
                    if not rows:
                        print("No sessions found.")
                    elif selected := picker(conn, rows, query):
                        launch(selected)
            elif args.command == "open":
                sync(conn, sources)
                launch(resolve(conn, args.id), args.dry_run)
            elif args.command in ("bookmark", "bm", "unbookmark", "unbm"):
                sync(conn, sources)
                row = resolve(conn, args.id)
                adding = args.command in ("bookmark", "bm")
                set_bookmark(conn, row, adding)
                if adding and args.text:
                    conn.execute("UPDATE sessions SET note=? WHERE agent=? AND sid=?",
                                 (clean(" ".join(args.text), 500), row["agent"], row["sid"]))
                    conn.commit()
                print(f"{'Bookmarked' if adding else 'Removed bookmark from'} {row['agent']}:{row['sid']}.")
            else:
                sync(conn, sources)
                row = resolve(conn, args.id)
                field = {"rename": "custom_title", "title": "custom_title", "note": "note", "tag": "tags"}[args.command]
                text = clean(" ".join(args.text), 500)
                if args.command == "tag":
                    text = " ".join(dict.fromkeys((row["tags"] + " " + text).split()))
                conn.execute(f"UPDATE sessions SET {field}=? WHERE agent=? AND sid=?", (text, row["agent"], row["sid"]))
                conn.commit()
                print(f"Updated {row['agent']}:{row['sid']}.")
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"agent-sessions: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
