# Agent notes for Session Hop

## Tests

```bash
python3 -m unittest
```

## Release process

Releases are semver tags (`vX.Y.Z`) on `main`. Before 1.0, a release with new features or breaking changes bumps the minor version. A release with only fixes bumps the patch version.

Only Homebrew installs follow release tags. Installs from a git clone, and the agent skill installed with `npx skills add`, track `main`.

### Where the formula lives

The Homebrew formula lives only in [iodic/homebrew-tap](https://github.com/iodic/homebrew-tap) at `Formula/session-hop.rb`. This repo has no copy of it. `brew install iodic/tap/session-hop` reads that formula, which clones this repo at the release tag. It also checks that the tag still resolves to the pinned commit:

```ruby
url "https://github.com/iodic/session-hop.git",
    using:    :git,
    tag:      "v0.2.0",
    revision: "b7f5ccaf3c0876a9dce287ee31bd965cce48fd17"
```

Homebrew takes the version number from the tag, so the formula has no `version` line.

### Steps

1. Check that `main` is clean and up to date, and that the tests pass.
2. Pick the next version from the highest `v*` tag (`git tag --sort=-v:refname | head -1`) and the commits since it.
3. Set `"version"` in `package.json` and commit it as `chore(release): bump version to X.Y.Z`.
4. Create an annotated tag: `git tag -a vX.Y.Z -m vX.Y.Z`.
5. Push `main` first, then the tag: `git push origin main && git push origin vX.Y.Z`.
6. Pushing the tag starts `.github/workflows/homebrew-tap.yml`. That workflow:
   - resolves the tag to its commit,
   - rewrites `tag:` and `revision:` in the tap's formula and commits `feat(formula): update Session Hop to X.Y.Z`,
   - runs `brew audit`, `brew install` and `brew test` on macOS,
   - pushes the commit to the tap only if those checks pass.
7. Confirm the run succeeded (`gh run list --workflow homebrew-tap.yml`), then check the result locally:

   ```bash
   brew update && brew upgrade iodic/tap/session-hop && brew list --versions session-hop
   ```

If the workflow fails, the tap is left unchanged. Fix the cause and rerun it for the same tag:

```bash
gh workflow run homebrew-tap.yml -f tag=vX.Y.Z -f push=true
```

Leave out `-f push=true` for a dry run. A dry run audits, installs and tests the formula, but doesn't push it.

Never move or reuse a published tag. If a release is wrong, release a new patch version.

### Changing the formula itself

The workflow only rewrites `tag:` and `revision:`. For any other formula change (dependencies, install steps, the test block), edit `Formula/session-hop.rb` in the tap directly. Before pushing, run:

```bash
brew audit --formula iodic/tap/session-hop
brew reinstall iodic/tap/session-hop && brew test iodic/tap/session-hop
```

The local tap checkout is at `$(brew --repository)/Library/Taps/iodic/homebrew-tap`. Its `origin` remote uses HTTPS, which has no stored credentials. Push over SSH instead: `git push git@github.com:iodic/homebrew-tap.git main`.

Don't set `HOMEBREW_NO_INSTALL_FROM_API` while testing. It makes Homebrew clone all of homebrew-core, which takes minutes.

### Workflow credentials

The workflow pushes to the tap with a deploy key. The key's public half is a write-enabled deploy key on `iodic/homebrew-tap`, and its private half is this repo's `HOMEBREW_TAP_DEPLOY_KEY` secret. To rotate it, generate a new ed25519 key, then replace both halves.
