# Session Hop

Hop back into local Pi and Claude Code conversations across projects: search, pick, resume. The command is `hop`. Python 3.11+, standard library only. It reads session files; it does not change them or call an AI model.

```sh
hop billbee                  # interactive picker; Enter resumes
hop                          # picker over every session
hop billbee -p .             # only this checkout's sessions
hop -p fizzy                 # only projects whose name contains "fizzy"
hop -b                       # only bookmarked sessions
hop bm 01a0 "finish tests"   # bookmark, optionally with a note
hop unbm 01a0
hop find "billing stripe"    # plain list output
hop open pi:01a0da86         # accepts a unique ID prefix
hop open claude:0c2f90be --dry-run
hop rename pi:01a0da86 "Fix Stripe webhooks"
hop note pi:01a0da86 "Waiting on production credentials"
hop tag pi:01a0da86 payments
hop sync
```

Words without a command open the picker (`pick` is the explicit name). Like `fzf --height`, it draws below the prompt instead of taking over the screen, and erases itself on exit. It filters as you type: ↑/↓ or Ctrl-P/Ctrl-N move, PgUp/PgDn page, Backspace, Ctrl-W, and Ctrl-U edit the query, Tab toggles a bookmark, Enter resumes, Esc or Ctrl-C quits. Colors are the terminal's own palette slots, so they follow its theme; set `NO_COLOR` to turn them off. When stdin or stdout isn't a terminal, it prints the list instead. To search for a word that is also a command name, use `hop -- sync` or `find sync`.

Each scan records a session's project: the nearest Git checkout containing its working directory, or the directory itself (your home directory never counts as a checkout). `-p` accepts a path (`.`, `~/work/app`) to show sessions under that checkout, or a bare word matched against project names. `-p` and `-b` work with `pick` and `find`, and search words also match project paths. Bookmarks (★) mark sessions to come back to; `bookmark`/`unbookmark` are the long forms of `bm`/`unbm`.

All commands automatically scan for changed session files. `sync` explicitly reports the number indexed. Search matches words in any order across titles, descriptions, tags, IDs, agent names, and project paths. An ID prefix doesn't need the agent unless it's ambiguous; then use `agent:<id>`. `open` changes to the original working directory and executes `pi --session <session-file>` or `claude --resume <id>`. The dry-run form prints a shell-safe equivalent.

Session names set inside Pi or Claude take priority, followed by Claude's generated title, then a shortened first user request. The description is a short user-request excerpt, **not** a reliable summary of the whole conversation. Use `rename` for the title and `note` for the description; `title` remains an alias for `rename`. Manual titles, notes, and tags survive rescans. Renaming here changes only the index, not the agent's own session name.

The SQLite index lives at `~/.local/share/session-hop/index.sqlite3` (or under `$XDG_DATA_HOME`); an index from the old `agent-sessions` name moves there on first run. It contains working directories and short prompt excerpts, so treat it as private. A new database is created with mode `0600`; no full transcripts, tool results, or model responses are saved in it. If the source session file is removed, the next scan removes its entry. Unavailable source directories are not pruned. Nothing is sent to Obsidian or any external service.

Run `python3 -m unittest discover -s . -p 'test_*.py'` from this directory to test with synthetic sessions. The CLI accepts `--db`, `--pi-dir`, and `--claude-dir` before the subcommand for testing or alternate storage.
