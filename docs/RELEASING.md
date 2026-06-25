# Releasing Jack

Releases are **tag-driven**. Pushing a `vX.Y.Z` tag makes CI (Linux only) build the
Python **engine** (wheel + sdist) and create the GitHub Release. The macOS **orb
app** (`.dmg`) is built **locally on a Mac** and uploaded to that same Release —
we keep it out of CI because macOS runner minutes are ~10× the cost. The version
lives in three manifests that must agree; the workflow refuses to publish on a
mismatch.

## Cut a release

The **git tag is the source of truth** for the version — you don't bump the
manifests by hand. Just tag and push; CI sets `pyproject.toml`, `Cargo.toml`,
`tauri.conf.json` and `Cargo.lock` from the tag, builds the engine, creates the
Release, and **commits that bump back to `main`** for you (so there's no manual
version/lockfile commit, and a forgotten bump can never skip a release).

```bash
# 1. (Optional) Refresh the changelog, then COMMIT it. Skip the whole step if you
#    don't want a changelog entry this release — there's nothing else to commit
#    (CI does the version bump for you), so a release can be just a tag.
# prepends a section to CHANGELOG.md
make changelog VERSION=0.5.1

# review and commit
git add CHANGELOG.md && git commit -m "docs: changelog for v0.5.1"

# 2. Tag and push. The TAG is the trigger; `--tags` pushes it. Pushing `main` just
#    keeps origin/main current (so it includes any changelog commit AND matches the
#    tagged commit — CI commits the version bump back onto main). With no commit in
#    step 1, the `main` push is a harmless no-op and only the tag goes up.
git tag v0.5.1
git push origin main --tags

# 3. CI gates the checks, sets the version FROM the tag, builds the engine wheel,
#    creates the GitHub Release, and pushes a "chore(release): v0.5.1 [skip ci]"
#    commit with the bumped manifests back to main.

# 4. On your Mac, pull that bump, then build + attach the single .dmg:
git pull                            # picks up CI's version bump (so the .dmg is versioned right)
make bundle                         # freeze engine -> sidecar -> the .dmg
make publish-orb VERSION=0.5.1      # uploads the .dmg AND sets the release notes
```

`make release VERSION=x` (local manifest bump) and `make release-check VERSION=x`
still exist if you ever want to bump/verify by hand, but the tagged release no
longer needs them — CI is authoritative.

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

1. **gate** (ubuntu) — ruff, format, mypy, pytest. The manifest-vs-tag check runs
   here too but is **non-blocking** (a warning): the tag is authoritative, so a
   version mismatch must never abort a tagged release.
2. **release** (ubuntu) — sets the manifests from the tag (`bump_version.py`),
   `uv build` makes the engine wheel/sdist, `softprops/action-gh-release` creates
   the GitHub Release, and the bump is committed back to the default branch as
   `chore(release): vX [skip ci]`.

The orb `.dmg` is added by `make publish-orb` (step 4) on a Mac, which needs the
GitHub CLI (`gh auth login`) and `cargo`/`tauri-cli` installed locally. Branch
protection note: CI pushes the bump commit with the default `GITHUB_TOKEN`, so
direct pushes to the default branch must be allowed for the bot (or swap in a PAT).

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
