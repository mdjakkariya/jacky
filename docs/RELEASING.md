# Releasing Autobot

Releases are **tag-driven**. Pushing a `vX.Y.Z` tag makes CI (Linux only) build the
Python **engine** (wheel + sdist) and create the GitHub Release. The macOS **orb
app** (`.dmg`) is built **locally on a Mac** and uploaded to that same Release —
we keep it out of CI because macOS runner minutes are ~10× the cost. The version
lives in three manifests that must agree; the workflow refuses to publish on a
mismatch.

## Cut a release

```bash
# 1. Bump every manifest (does NOT commit/tag for you).
make release VERSION=0.3.0          # -> pyproject.toml, Cargo.toml, tauri.conf.json

# 2. Update the changelog from the Conventional Commits since the last tag.
make changelog VERSION=0.3.0        # prepends a categorized section to CHANGELOG.md
#   (preview first with: make changelog-preview)

# 3. Review the diff, then commit + tag + push — this triggers CI.
git add -A && git commit -m "chore(release): v0.3.0"
git tag v0.3.0
git push origin main --tags

# 4. CI gate + builds the engine wheel and creates the Release. Then, on your Mac,
#    build the single .dmg (orb + embedded engine) and attach it to that Release:
make bundle                         # freeze engine -> sidecar -> the .dmg
make publish-orb VERSION=0.3.0      # uploads the .dmg AND sets the release notes
```

## Changelog (automated)

The changelog is generated from **Conventional Commits** by
[git-cliff](https://git-cliff.org) (`brew install git-cliff`), configured in
[`cliff.toml`](../cliff.toml):

- Write commits as `feat: …`, `fix: …`, `perf: …`, `refactor: …`, `docs: …`,
  `chore: …` (a `!` like `feat!:` or a `BREAKING CHANGE:` footer marks a breaking
  change). These map to the **Features / Bug Fixes / Performance & Stability /
  Improvements / Documentation / Maintenance** sections. `chore(release):` commits
  are skipped, and non-conventional commits are left out of the changelog.
- `make changelog VERSION=x` prepends that version's section to
  [`CHANGELOG.md`](../CHANGELOG.md) (run once per release, then commit it).
- `make publish-orb` regenerates the same notes for the GitHub Release body
  automatically — so the Release page and `CHANGELOG.md` stay in sync with zero
  manual writing. (If `git-cliff` isn't installed it falls back to the plain
  "dev preview" note.)

`make bundle` is the single-installable build: it freezes the engine
(`make freeze`), drops it in as the orb's sidecar, and runs `cargo tauri build`,
producing one `.dmg` that contains both. See [`PACKAGING.md`](PACKAGING.md).

Pushing the tag triggers `.github/workflows/release.yml`:

1. **gate** (ubuntu) — ruff, format, mypy, pytest, and `bump_version.py --check`
   confirming all three manifests equal the tag. Any failure aborts the release.
2. **release** (ubuntu) — `uv build` makes the engine wheel/sdist and
   `softprops/action-gh-release` creates the GitHub Release with them attached.

The orb `.dmg` is added by `make publish-orb` (step 3), which needs the GitHub CLI
(`gh auth login`) and `cargo`/`tauri-cli` installed locally.
`make release-check VERSION=0.2.0` verifies the manifests before tagging.

## Versioning

Semantic Versioning (`MAJOR.MINOR.PATCH`). The git tag (`vX.Y.Z`) is the source of
truth; the manifests are kept in lockstep by `scripts/bump_version.py`.

## Signing (current status: unsigned)

The `.dmg` is **unsigned** — fine for a dev preview. macOS Gatekeeper warns the
first time; users right-click the app → **Open** → Open. To distribute without the
warning later, add an Apple Developer ID cert + notarization secrets and pass them
to `tauri-action` (`APPLE_CERTIFICATE`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`).

## What a developer needs to run a release build

The engine isn't fully standalone — it still needs the runtime pieces:

- **Ollama** running locally (default), *or* an Anthropic API key (cloud mode).
- The STT model (faster-whisper downloads on first run; whisper.cpp via the
  `whispercpp` extra) and a Piper voice for TTS.
- macOS **Microphone** permission, and **Automation** (Finder) permission for
  `empty_trash`.

See the README "Try a release" section for the user-facing steps.
