"""Local index of resumable Pi and Claude Code conversations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
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
            PRIMARY KEY(agent, sid)
        );
        CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated DESC);
        CREATE INDEX IF NOT EXISTS sessions_path ON sessions(source_path);
    """)
    return conn


def sync(conn: sqlite3.Connection, sources: dict[str, Path]) -> tuple[int, int]:
    changed = 0
    removed = 0
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
                conn.execute("""
                    INSERT INTO sessions(agent, sid, cwd, title, description, source_path, mtime_ns, size, updated)
                    VALUES (:agent, :sid, :cwd, :title, :description, :path, :mtime_ns, :size, :updated)
                    ON CONFLICT(agent, sid) DO UPDATE SET
                        cwd=excluded.cwd, title=excluded.title, description=excluded.description,
                        source_path=excluded.source_path, mtime_ns=excluded.mtime_ns,
                        size=excluded.size, updated=excluded.updated
                """, {**info, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "updated": stat.st_mtime})
                changed += 1
            except (OSError, ValueError, TypeError):
                continue  # A deleted or malformed file does not stop the rest of the scan.
        for row in conn.execute("SELECT sid, source_path FROM sessions WHERE agent=?", (agent,)).fetchall():
            if row["source_path"] not in seen:
                conn.execute("DELETE FROM sessions WHERE agent=? AND sid=?", (agent, row["sid"]))
                removed += 1
    conn.commit()
    return changed, removed


def search(conn: sqlite3.Connection, query: str = "", limit: int = 30) -> list[sqlite3.Row]:
    terms = query.casefold().split()
    matches = []
    for row in conn.execute("SELECT *, COALESCE(custom_title, title) AS display_title, "
                            "COALESCE(note, description) AS display_note FROM sessions ORDER BY updated DESC"):
        haystack = " ".join(str(row[k]) for k in ("agent", "sid", "cwd", "display_title", "display_note", "tags")).casefold()
        if all(term in haystack for term in terms):
            matches.append(row)
            if len(matches) >= limit:
                break
    return matches


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


def format_row(row: sqlite3.Row) -> str:
    date = datetime.fromtimestamp(row["updated"]).strftime("%Y-%m-%d")
    return f"{row['agent']}:{row['sid'][:12]:12}  {date}  {Path(row['cwd']).name or row['cwd']}  {row['display_title']}"


def picker(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not rows:
        print("No sessions found.")
        return None
    if not sys.stdin.isatty():
        raise ValueError("The picker requires a terminal. Use 'find' then 'open <id>' instead.")
    for i, row in enumerate(rows, 1):
        print(f"{i:>3}  {format_row(row)}")
    try:
        choice = input("Session number (empty to cancel): ").strip()
    except EOFError:
        return None
    if not choice:
        return None
    if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
        raise ValueError("Invalid selection.")
    return rows[int(choice) - 1]


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find and resume Pi and Claude Code sessions")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite index path")
    parser.add_argument("--pi-dir", type=Path, default=DEFAULT_PI, help="Pi session directory")
    parser.add_argument("--claude-dir", type=Path, default=DEFAULT_CLAUDE, help="Claude projects directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Index new and changed sessions")
    find = sub.add_parser("find", help="Search sessions, newest first")
    find.add_argument("query", nargs="*", help="Words in title, note, path, tags, or ID")
    find.add_argument("--limit", type=int, default=30)
    pick = sub.add_parser("pick", help="Choose a session and resume it")
    pick.add_argument("query", nargs="*")
    pick.add_argument("--limit", type=int, default=30)
    opening = sub.add_parser("open", help="Resume a session by ID prefix")
    opening.add_argument("id")
    opening.add_argument("--dry-run", action="store_true", help="Print the shell command instead")
    for verb in ("title", "note", "tag"):
        cmd = sub.add_parser(verb, help=f"Set a manual {verb} on a session")
        cmd.add_argument("id")
        cmd.add_argument("text", nargs="+", help="New text")
    args = parser.parse_args(argv)
    if args.command in ("find", "pick") and args.limit < 1:
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
                rows = search(conn, " ".join(args.query), args.limit)
                if args.command == "find":
                    for row in rows:
                        print(format_row(row))
                        print(f"     {row['display_note']}")
                    if not rows:
                        print("No sessions found.")
                else:
                    selected = picker(rows)
                    if selected:
                        launch(selected)
            elif args.command == "open":
                sync(conn, sources)
                launch(resolve(conn, args.id), args.dry_run)
            else:
                sync(conn, sources)
                row = resolve(conn, args.id)
                field = {"title": "custom_title", "note": "note", "tag": "tags"}[args.command]
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
