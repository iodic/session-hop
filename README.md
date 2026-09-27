# agent-sessions

Search and resume local Pi and Claude Code conversations across projects. Python 3.11+, standard library only. It reads session files; it does not change them or call an AI model.

```sh
./agent-sessions sync
./agent-sessions find "billing"
./agent-sessions find "billing stripe"
./agent-sessions pick "billing"            # numbered terminal picker, then resume
./agent-sessions open pi:01a0da86         # accepts a unique ID prefix
./agent-sessions open claude:0c2f90be --dry-run
./agent-sessions rename pi:01a0da86 "Fix Stripe webhooks"
./agent-sessions note pi:01a0da86 "Waiting on production credentials"
./agent-sessions tag pi:01a0da86 payments
```

`find`, `pick`, and `open` automatically scan for changed session files. `sync` explicitly reports the number indexed. Search matches words in any order across titles, descriptions, tags, IDs, agent names, and project paths. Use `agent:<id>` if the ID prefix isn't unique. `open` changes to the original working directory and executes `pi --session <session-file>` or `claude --resume <id>`. The dry-run form prints a shell-safe equivalent.

Session names set inside Pi or Claude take priority, followed by Claude's generated title, then a shortened first user request. The description is a short user-request excerpt, **not** a reliable summary of the whole conversation. Use `rename` for the title and `note` for the description; `title` remains an alias for `rename`. Manual titles, notes, and tags survive rescans. Renaming here changes only the index, not the agent's own session name.

The SQLite index lives at `~/.local/share/agent-sessions/index.sqlite3` (or under `$XDG_DATA_HOME`). It contains working directories and short prompt excerpts, so treat it as private. A new database is created with mode `0600`; no full transcripts, tool results, or model responses are saved in it. If the source session file is removed, the next scan removes its entry. Unavailable source directories are not pruned. Nothing is sent to Obsidian or any external service.

Run `python3 -m unittest discover -s . -p 'test_*.py'` from this directory to test with synthetic sessions. The CLI accepts `--db`, `--pi-dir`, and `--claude-dir` before the subcommand for testing or alternate storage.
