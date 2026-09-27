"""Session Hop: find and resume local Pi and Claude Code conversations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import shlex
import sqlite3
import sys
import termios
import time
import tty
import unicodedata
from datetime import datetime

DATA_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
DATA_DIR = DATA_ROOT / "session-hop"
LEGACY_DATA_DIR = DATA_ROOT / "agent-sessions"  # Before the Session Hop rename.
DEFAULT_DB = DATA_DIR / "index.sqlite3"
DEFAULT_PI = Path.home() / ".pi/agent/sessions"
DEFAULT_CLAUDE = Path.home() / ".claude/projects"
PARSER_VERSION = 1  # Bump when parse_session output changes to re-read unchanged files.


def clean(text: str, limit: int) -> str:
    # Single-line metadata only. Never copy a transcript into the index.
    return re.sub(r"\s+", " ", text).strip()[:limit]


SKILL_BLOCK = re.compile(r'<skill name="([^"]*)"[^>]*>.*?</skill>', re.S)
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Harness output echoed into user turns: command output, shell escapes, notifications, markers.
HARNESS_NOISE = re.compile(
    r"<(local-command-[\w-]+|bash-[\w-]+|task-notification|system-reminder)>.*?</\1>"
    r"|<!--.*?-->|\[Image #\d+\]|\[Request interrupted by user[^\]]*\]|\[Extension issues\].*", re.S)


def prompt_text(text: str) -> str:
    """What the person actually typed, without the wrappers agents put around it."""
    command = COMMAND_NAME.search(text)
    if command:
        # A bare "/clear" or "/model" says nothing about the conversation; keep only commands with arguments.
        args = COMMAND_ARGS.search(text)
        args = args[1].strip() if args else ""
        return f"{command[1].strip()} {args}" if args else ""
    text = SKILL_BLOCK.sub(lambda match: f"/{match[1]} ", text)
    return HARNESS_NOISE.sub(" ", text)


def raw_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    return ""


def parse_session(agent: str, path: Path) -> dict | None:
    sid = ""
    cwd = ""
    first = ""
    last = ""
    name = ""
    ai_title = ""
    command = ""  # A bare "/qa" is the title of last resort, if the agent went on to answer it.
    replied = False

    def take(content: object) -> None:
        nonlocal first, last, command
        raw = raw_text(content)
        # Wrappers go before truncation so a long skill block can't crowd out the request after it.
        text = clean(prompt_text(raw), 280)
        if text:
            first = first or text
            last = text
        elif not command and (match := COMMAND_NAME.search(raw)):
            command = clean(match[1], 100)

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
                        take(message.get("content"))
                    elif isinstance(message, dict) and message.get("role") == "assistant":
                        replied = True
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
                        take(message.get("content"))
                elif kind == "assistant" and not entry.get("isSidechain"):
                    replied = True
    first = first or (command if replied else "")
    last = last or first
    if agent == "claude":
        sid = sid or path.stem
    if not isinstance(sid, str) or not isinstance(cwd, str) or not cwd or not (first or name or ai_title):
        return None
    title = clean(name or ai_title or first, 100)
    # This is an excerpt, not an AI-generated summary. Manual notes override it.
    description = first if name or ai_title else (last if last != first else first)
    return {"agent": agent, "sid": sid, "cwd": cwd, "title": title,
            "description": clean(description, 280), "path": str(path)}


def migrate_legacy_data() -> None:
    # An index from before the rename moves over once, keeping titles, notes, tags, and bookmarks.
    if LEGACY_DATA_DIR.is_dir() and not DATA_DIR.exists():
        LEGACY_DATA_DIR.rename(DATA_DIR)


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
    # When parsing improves, forget file stamps so the next scan re-reads every session.
    # Manual titles, notes, tags, and bookmarks are untouched.
    if conn.execute("PRAGMA user_version").fetchone()[0] < PARSER_VERSION:
        conn.execute("UPDATE sessions SET mtime_ns=0")
        conn.execute(f"PRAGMA user_version={PARSER_VERSION}")
        conn.commit()
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
                    # Nothing worth resuming, such as a lone "/clear". Drop a stale entry unless annotated.
                    removed += conn.execute(
                        "DELETE FROM sessions WHERE agent=? AND source_path=? AND custom_title IS NULL "
                        "AND note IS NULL AND tags='' AND bookmarked=0", (agent, str(path))).rowcount
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


def age(timestamp: float, now: float) -> str:
    seconds = max(0, now - timestamp)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d"
    return datetime.fromtimestamp(timestamp).strftime("%b %d" if seconds < 300 * 86400 else "%Y")


def cells(text: str) -> int:
    # Terminal columns: wide East Asian characters and most emoji take two.
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in text)


def clip(text: str, room: int) -> str:
    used = 0
    for i, char in enumerate(text):
        used += cells(char)
        if used > room:
            return text[:i]
    return text


def fit(text: str, width: int) -> str:
    # Pads by terminal columns so wide characters don't push the age column off the edge.
    if cells(text) > width:
        text = clip(text, width - 1) + "…"
    return text + " " * (width - cells(text))


def highlight(text: str, terms: list[str], style: str, hit: str) -> list[tuple[str, str]]:
    # Split text into segments so every occurrence of a search word stands out.
    marked = [False] * len(text)
    folded = text.lower()
    for term in terms:
        start = folded.find(term)
        while term and start != -1:
            marked[start:start + len(term)] = [True] * len(term)
            start = folded.find(term, start + len(term))
    segments: list[tuple[str, str]] = []
    for char, on in zip(text, marked):
        current = hit if on else style
        if segments and segments[-1][1] == current:
            segments[-1] = (segments[-1][0] + char, current)
        else:
            segments.append((char, current))
    return segments


# Styles are SGR parameters naming the terminal's own palette slots (33 = its yellow,
# 34 = its blue, ...) rather than RGB values, so the terminal theme decides every actual
# color. Yellow is the accent: most themes put their signature warm tone there.
ACCENT, PROJECT, PATH, AGE, HIT, DIM, NOTE, BOLD = "1;33", "1;34", "34", "36", "1;4;33", "2", "3", "1"
AGENT = "33"  # The accent's hue without its weight, so agent names don't compete with the title.
# The selected row's band: bright black is a muted gray on dark themes but near-black on light
# ones, where the "white" slot is the muted gray instead.
DARK_BAND, LIGHT_BAND = "100", "47"
COLOR_CODE = re.compile(r"^(3|4|9|10)\d$")


def paint(segments: list[tuple[str, str]], width: int, band: str = "", color: bool = True) -> str:
    """One screen line: styled segments clipped to width, the selected row padded into a band."""
    band = band and (band if color else "7")
    out = []
    used = 0
    for text, style in segments:
        text = clip(text, width - used)
        if not text:
            continue  # An empty segment, such as a blank query, must not hide the ones after it.
        codes = [code for code in style.split(";") if code
                 and not (band and code == DIM) and (color or not COLOR_CODE.match(code))]
        if band:
            codes.append(band)
        out.append(f"\x1b[0;{';'.join(codes)}m{text}" if codes else f"\x1b[0m{text}")
        used += cells(text)
    if band and used < width:
        out.append(f"\x1b[0;{band}m" + " " * (width - used))
    return "".join(out) + "\x1b[0m"


KEY_NAMES = {"\x1b[A": "up", "\x1bOA": "up", "\x10": "up", "\x1b[B": "down", "\x1bOB": "down", "\x0e": "down",
             "\x1b[5~": "pageup", "\x1b[6~": "pagedown", "\r": "enter", "\n": "enter", "\t": "tab",
             "\x7f": "backspace", "\x08": "backspace", "\x15": "clear", "\x17": "word",
             "\x1b": "esc", "\x03": "esc", "\x07": "esc"}
ESCAPE_SEQUENCE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.)")


def split_keys(data: str) -> list[str]:
    """Key names or typed text from one read, which can hold several keys or a paste."""
    keys = []
    i = 0
    while i < len(data):
        sequence = ESCAPE_SEQUENCE.match(data, i)
        chunk = sequence[0] if sequence else data[i]
        if chunk in KEY_NAMES:
            keys.append(KEY_NAMES[chunk])
        elif chunk.isprintable():
            keys.append(chunk)
        i += len(chunk)  # Unknown escape sequences and control characters are ignored.
    return keys


class Picker:
    """Filter-as-you-type session list. Renders to strings so it can be tested without a terminal."""

    HINTS = (("↑↓", "move"), ("⏎", "open"), ("tab", "bookmark"), ("esc", "quit"))

    def __init__(self, rows: list[dict], query: str = "", on_bookmark=None, scope: str = "", color: bool = True):
        self.rows = rows
        self.query = query
        self.on_bookmark = on_bookmark
        self.scope = scope
        self.color = color
        self.band = DARK_BAND
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

    def handle(self, key: str) -> str | None:
        if key == "enter":
            return "open" if self.visible else None
        if key == "esc":
            return "quit"
        if key in ("up", "down", "pageup", "pagedown"):
            self.move({"up": -1, "down": 1, "pageup": -self.page, "pagedown": self.page}[key])
        elif key == "tab":
            row = self.selected
            if row and self.on_bookmark:
                self.on_bookmark(row, not row["bookmarked"])
        elif key == "backspace":
            self.query = self.query[:-1]
            self.refilter()
        elif key == "clear":
            self.query = ""
            self.refilter()
        elif key == "word":
            self.query = re.sub(r"\S*\s*$", "", self.query)
            self.refilter()
        elif len(key) == 1:
            self.query += key
            self.refilter()
        return None

    @staticmethod
    def title_width(width: int) -> int:
        # Star, agent, project and their gaps on the left; age right-aligned at the edge.
        return max(5, width - 2 - 9 - 20 - 5)

    def row_segments(self, row: dict, chosen: bool, now: float, width: int) -> list[tuple[str, str]]:
        """Selection is every column in bold, plus a colored age; no band or bar."""
        project = Path(row["project"] or row["cwd"]).name or row["cwd"]
        title = BOLD if chosen else ""
        return [
            ("★ " if row["bookmarked"] else "  ", ACCENT),
            (f"{row['agent']:<9}", f"{BOLD};{AGENT}" if chosen else AGENT),
            (fit(project, 18) + "  ", PROJECT if chosen else PATH),
            *highlight(fit(row["display_title"], self.title_width(width)), self.query.lower().split(),
                       title, HIT + (";1" if chosen else "")),
            (f"{age(row['updated'], now):>5}", f"{BOLD};{AGE}" if chosen else DIM),
        ]

    def render(self, width: int, height: int) -> tuple[list[str], int]:
        """Screen lines (prompt, column names, rows, gap, detail, note, hints) and the prompt's cursor column."""
        width = max(10, width - 1)  # Never touch the last column, where terminals wrap.
        self.page = max(1, height - 6)
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + self.page:
            self.top = self.index - self.page + 1
        self.top = max(0, min(self.top, len(self.visible) - self.page))

        info = [(f"{self.scope}  ", PROJECT)] if self.scope else []
        info.append((f"{len(self.visible)}/{len(self.rows)}", DIM))
        prompt = [("❯ ", ACCENT), (self.query, BOLD)]
        gap = width - sum(cells(text) for text, _ in prompt + info)
        lines = [self.paint(prompt + ([(" " * gap, "")] + info if gap > 0 else []), width),
                 self.paint([(f"  {'agent':<9}{fit('project', 18)}  {fit('title', self.title_width(width))}{'age':>5}",
                              DIM)], width)]

        now = datetime.now().timestamp()
        for i in range(self.top, self.top + self.page):
            if i < len(self.visible):
                chosen = i == self.index
                lines.append(self.paint(self.row_segments(self.visible[i], chosen, now, width), width))
            elif i == 0:
                lines.append(self.paint([("  No matching sessions", DIM + ";" + NOTE)], width))
            else:
                lines.append("")

        # Everything below the prompt lines up with the cursor, after the star gutter.
        indent = " " * 2
        lines.append("")
        row = self.selected
        if row:
            lines.append(self.paint([(indent + f"{row['agent']}:{row['sid'][:8]}", AGENT), ("  ·  ", DIM),
                                     (row["cwd"].replace(str(Path.home()), "~", 1), PATH), ("  ·  ", DIM),
                                     (age(row["updated"], now), AGE)], width))
            lines.append(self.paint([(indent + fit(row["display_note"], width - len(indent)).rstrip(),
                                      DIM + ";" + NOTE)], width))
        else:
            lines += ["", ""]
        hints = [(indent, "")]
        for i, (key, label) in enumerate(self.HINTS):
            hints += [(key, BOLD), (f" {label}", DIM), ("   " if i < len(self.HINTS) - 1 else "", "")]
        lines.append(self.paint(hints, width))
        return lines, min(width, cells("❯ " + self.query))

    def paint(self, segments: list[tuple[str, str]], width: int, band: str = "") -> str:
        return paint(segments, width, band, self.color)


def read_keys(fd: int) -> list[str]:
    data = os.read(fd, 1024)
    if data == b"\x1b" and select.select([fd], [], [], 0.03)[0]:
        data += os.read(fd, 1024)  # Arrow keys can arrive split from their Esc prefix.
    while True:
        try:
            return split_keys(data.decode())
        except UnicodeDecodeError:
            data += os.read(fd, 1)  # A multi-byte character was split across reads.


def luminance(reply: bytes) -> float | None:
    """Brightness 0-1 of an OSC 11 background-color reply such as ESC ] 11;rgb:f8f8/f9f9/fafa."""
    match = re.search(rb"\]11;rgba?:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not match:
        return None
    red, green, blue = (int(part, 16) / (16 ** len(part) - 1) for part in match.groups())
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def light_background(fd_in: int, fd_out: int) -> tuple[bool, bytes]:
    """Ask the terminal for its background (OSC 11), so the band suits light and dark themes.

    Device attributes (DA1) follow as a sentinel every terminal answers, so an unsupported query
    costs no timeout. Returns whether the theme is light, plus any keys typed meanwhile.
    """
    os.write(fd_out, b"\x1b]11;?\x07\x1b[c")
    data = b""
    deadline = time.monotonic() + 0.5
    while not re.search(rb"\x1b\[\?[0-9;]*c", data) and (left := deadline - time.monotonic()) > 0:
        if select.select([fd_in], [], [], left)[0]:
            data += os.read(fd_in, 1024)
    level = luminance(data)
    typed = re.sub(rb"\x1b\]11;[^\x07\x1b]*(\x07|\x1b\\)|\x1b\[\?[0-9;]*c", b"", data)
    if level is None:
        # COLORFGBG is "fg;bg" in palette slots; 7 and 15 are the white slots.
        level = 1.0 if os.environ.get("COLORFGBG", "").split(";")[-1] in ("7", "15") else 0.0
    return level > 0.5, typed


def picker(conn: sqlite3.Connection, rows: list[dict], query: str = "", scope: str = "") -> dict | None:
    """fzf-style inline picker: draws a few lines below the prompt and erases them on exit."""
    color = not os.environ.get("NO_COLOR")
    state = Picker(rows, query, lambda row, value: set_bookmark(conn, row, value), scope, color)
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    size = os.get_terminal_size(fd_out)
    height = max(7, min(size.lines - 1, len(rows) + 6, max(12, size.lines * 2 // 5)))

    def write(text: str) -> None:
        os.write(fd_out, text.encode())

    saved = termios.tcgetattr(fd_in)
    try:
        tty.setraw(fd_in)
        light, typed = light_background(fd_in, fd_out)
        state.band = LIGHT_BAND if light else DARK_BAND
        for key in split_keys(typed.decode(errors="ignore")):
            state.handle(key)
        # Scroll the screen up if needed so the block fits, then return to its first line.
        write("\x1b[?7l" + "\r\n" * (height - 1) + f"\x1b[{height - 1}A")
        while True:
            width = os.get_terminal_size(fd_out).columns
            lines, column = state.render(width, height)
            write("\x1b[?25l\r" + "\r\n".join("\x1b[2K" + line for line in lines)
                  + f"\x1b[{height - 1}A\r" + (f"\x1b[{column}C" if column else "") + "\x1b[?25h")
            for key in read_keys(fd_in):
                action = state.handle(key)
                if action == "quit":
                    return None
                if action == "open":
                    return state.selected
    finally:
        write("\r\x1b[J\x1b[?7h\x1b[?25h")
        termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)


def launch(row: sqlite3.Row, dry_run: bool = False) -> None:
    cwd = Path(row["cwd"])
    if not cwd.is_dir():
        raise ValueError(f"Project directory no longer exists: {cwd}")
    if not Path(row["source_path"]).is_file():
        raise ValueError("Session file no longer exists; run 'hop sync'.")
    argv = resume_argv(row)
    if dry_run:
        print(f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}")
        return
    os.chdir(cwd)
    os.execvp(argv[0], argv)


COMMANDS = {"sync", "find", "pick", "open", "rename", "title", "note", "tag", "bookmark", "bm", "unbookmark", "unbm"}
GLOBAL_OPTIONS = {"--db", "--pi-dir", "--claude-dir"}


def with_default_command(argv: list[str]) -> list[str]:
    # "hop billbee" means "hop pick billbee".
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
        prog="hop",
        description="Session Hop: find and resume Pi and Claude Code sessions",
        usage="hop [words ...] [-p PROJECT] [-b]\n       hop <command> ...",
        epilog="Without a command, words open the interactive picker: hop billbee",
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
        if args.db == DEFAULT_DB:
            migrate_legacy_data()
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
                    elif selected := picker(conn, rows, query, " · ".join(
                            filter(None, [args.project and f"project {args.project}",
                                          args.bookmarked and "★ bookmarked"]))):
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
        print(f"hop: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
