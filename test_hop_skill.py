import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parent / "skills" / "hop" / "scripts" / "bookmark-current.sh"


class HopSkillTests(unittest.TestCase):
    def run_script(self, session_env, *note):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "args.json"
            fake_hop = root / "hop"
            fake_hop.write_text(
                "#!/bin/sh\n"
                "python3 - \"$HOP_CAPTURE\" \"$@\" <<'PY'\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
                "PY\n"
            )
            fake_hop.chmod(0o755)
            env = {
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "HOP_CAPTURE": str(capture),
                **session_env,
            }
            result = subprocess.run([str(SCRIPT), *note], text=True, capture_output=True, env=env)
            args = json.loads(capture.read_text()) if capture.exists() else None
            return result, args

    def test_bookmarks_current_pi_session_with_note(self):
        result, args = self.run_script(
            {"PI_SESSION_ID": "pi-id", "PI_SESSION_FILE": "/tmp/pi.jsonl"},
            "finish the tests",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ["bookmark", "pi:pi-id", "finish the tests"])

    def test_detects_claude_and_codex_sessions(self):
        for env, expected in (
            ({"CLAUDE_CODE_SESSION_ID": "claude-id"}, "claude:claude-id"),
            ({"CODEX_THREAD_ID": "codex-thread"}, "codex:codex-thread"),
            ({"CODEX_SESSION_ID": "codex-session"}, "codex:codex-session"),
        ):
            with self.subTest(expected=expected):
                result, args = self.run_script(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args, ["bookmark", expected])

    def test_rejects_missing_and_ephemeral_sessions(self):
        result, args = self.run_script({})
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(args)
        self.assertIn("no current", result.stderr)

        result, args = self.run_script({"PI_SESSION_ID": "ephemeral"})
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(args)
        self.assertIn("ephemeral", result.stderr)


if __name__ == "__main__":
    unittest.main()
