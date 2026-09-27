class SessionHop < Formula
  include Language::Python::Shebang

  desc "Find and resume local Pi, Claude Code, and Codex sessions"
  homepage "https://github.com/iodic/session-hop"
  url "git@github.com:iodic/session-hop.git",
      using: :git,
      revision: "161c76594265858ff8fbd5d79981476a1cc75ab2"
  version "0.1.0"

  depends_on "python@3.14"

  def install
    libexec.install "hop", "session_hop.py"
    rewrite_shebang detected_python_shebang, libexec/"hop"
    bin.install_symlink libexec/"hop"
  end

  test do
    (testpath/"pi").mkpath
    (testpath/"claude").mkpath
    (testpath/"codex").mkpath
    (testpath/"pi/session.jsonl").write <<~JSONL
      {"type":"session","id":"brew-test","cwd":"#{testpath}"}
      {"type":"message","message":{"role":"user","content":"Test Homebrew installation"}}
    JSONL

    options = ["--db", (testpath/"index.sqlite3").to_s,
               "--pi-dir", (testpath/"pi").to_s,
               "--claude-dir", (testpath/"claude").to_s,
               "--codex-dir", (testpath/"codex").to_s]
    assert_match "1 total", shell_output("#{bin}/hop #{options.join(" ")} sync")
    assert_match "Updated pi:brew-test", shell_output("#{bin}/hop #{options.join(" ")} rename brew-test Renamed")
    assert_match "0 changed sessions", shell_output("#{bin}/hop #{options.join(" ")} sync")
  end
end
