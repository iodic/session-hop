# agent-sessions

Search and resume local Pi and Claude Code conversations across projects. Python 3.11+, standard library only. It reads session files; it does not change them or call an AI model.

```sh
./agent-sessions billbee                  # interactive picker; Enter resumes
./agent-sessions                          # picker over every session
./agent-sessions billbee -p .             # only this checkout's sessions
./agent-sessions -p fizzy                 # only projects whose name contains "fizzy"
./agent-sessions -b                       # only bookmarked sessions
./agent-sessions bm 01a0 "finish tests"   # bookmark, optionally with a note
./agent-sessions unbm 01a0
./agent-sessions find "billing stripe"    # plain list output
./agent-sessions open pi:01a0da86         # accepts a unique ID prefix
./agent-sessions open claude:0c2f90be --dry-run
./agent-sessions rename pi:01a0da86 "Fix Stripe webhooks"
./agent-sessions note pi:01a0da86 "Waiting on production credentials"
./agent-sessions tag pi:01a0da86 payments
./agent-sessions sync
```

Words without a command open the picker (`pick` is the explicit name). It filters as you type: ↑/↓ or Ctrl-P/Ctrl-N move, PgUp/PgDn page, Backspace and Ctrl-U edit the query, Tab toggles a bookmark, Enter resumes, Esc quits. When stdin or stdout isn't a terminal, it prints the list instead. To search for a word that is also a command name, use `agent-sessions -- sync` or `find sync`.

Each scan records a session's project: the nearest Git checkout containing its working directory, or the directory itself (your home directory never counts as a checkout). `-p` accepts a path (`.`, `~/work/app`) to show sessions under that checkout, or a bare word matched against project names. `-p` and `-b` work with `pick` and `find`, and search words also match project paths. Bookmarks (★) mark sessions to come back to; `bookmark`/`unbookmark` are the long forms of `bm`/`unbm`.

All commands automatically scan for changed session files. `sync` explicitly reports the number indexed. Search matches words in any order across titles, descriptions, tags, IDs, agent names, and project paths. An ID prefix doesn't need the agent unless it's ambiguous; then use `agent:<id>`. `open` changes to the original working directory and executes `pi --session <session-file>` or `claude --resume <id>`. The dry-run form prints a shell-safe equivalent.

Session names set inside Pi or Claude take priority, followed by Claude's generated title, then a shortened first user request. The description is a short user-request excerpt, **not** a reliable summary of the whole conversation. Use `rename` for the title and `note` for the description; `title` remains an alias for `rename`. Manual titles, notes, and tags survive rescans. Renaming here changes only the index, not the agent's own session name.

The SQLite index lives at `~/.local/share/agent-sessions/index.sqlite3` (or under `$XDG_DATA_HOME`). It contains working directories and short prompt excerpts, so treat it as private. A new database is created with mode `0600`; no full transcripts, tool results, or model responses are saved in it. If the source session file is removed, the next scan removes its entry. Unavailable source directories are not pruned. Nothing is sent to Obsidian or any external service.

Run `python3 -m unittest discover -s . -p 'test_*.py'` from this directory to test with synthetic sessions. The CLI accepts `--db`, `--pi-dir`, and `--claude-dir` before the subcommand for testing or alternate storage.
