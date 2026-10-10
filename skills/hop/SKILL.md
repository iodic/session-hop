---
name: hop
description: Bookmark the current Pi, Claude Code, or Codex session in Session Hop, optionally with a note. Use when the user asks to bookmark, save, pin, or mark the current agent session in Hop.
compatibility: Requires Python 3.11+ and Session Hop on PATH or in the containing package. The agent shell must expose PI_SESSION_ID, CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, or CODEX_SESSION_ID.
---

# Hop

Bookmark the active session, not another session from the index.

1. Treat the user's arguments as an optional bookmark note. Do not invent a note when they provided none.
2. Run `scripts/bookmark-current.sh` from this skill directory. Pass the note as arguments after the script path, preserving it as user text.
3. Report the script's result briefly. If it fails, show the error and do not guess a session ID or bookmark a different session.

Do not inspect or edit an agent's session files. The script reads the current agent's session ID from its environment and calls Session Hop.

Codex may block writes to Session Hop's index under `~/.local/share/session-hop`. If the command fails specifically because of sandbox permissions, rerun the same script with Codex's permission-escalation mechanism and let the user approve it. Do not change the index location as a workaround.
