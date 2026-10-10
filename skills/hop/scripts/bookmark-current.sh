#!/bin/sh
set -eu

if [ -n "${PI_SESSION_ID:-}" ]; then
    if [ -z "${PI_SESSION_FILE:-}" ]; then
        echo "hop: the current Pi session is ephemeral and cannot be bookmarked" >&2
        exit 1
    fi
    session="pi:$PI_SESSION_ID"
elif [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    session="claude:$CLAUDE_CODE_SESSION_ID"
elif [ -n "${CODEX_THREAD_ID:-}" ]; then
    session="codex:$CODEX_THREAD_ID"
elif [ -n "${CODEX_SESSION_ID:-}" ]; then
    session="codex:$CODEX_SESSION_ID"
else
    echo "hop: no current Pi, Claude Code, or Codex session ID was found" >&2
    exit 1
fi

if command -v hop >/dev/null 2>&1; then
    hop_bin=$(command -v hop)
else
    script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)
    hop_bin="$script_dir/../../../hop"
    if [ ! -x "$hop_bin" ]; then
        echo "hop: Session Hop is not installed; install it and make 'hop' available on PATH" >&2
        exit 1
    fi
fi

exec "$hop_bin" bookmark "$session" "$@"
