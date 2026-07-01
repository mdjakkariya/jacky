# Releasing Jack

Releases are **PR-bumped, tag-triggered**. The version bump lands on `main` through
a normal **PR** (branch protection blocks direct pushes — see below); pushing the
resulting `vX.Y.Z` tag then makes CI (Linux only) build the Python **engine**
(wheel + sdist) and create the GitHub Release. The macOS **orb app** (`.dmg`) is
built **locally on a Mac** and uploaded to that same Release — we keep it out of CI
because macOS runner minutes are ~10× the cost. The version lives in three manifests
that must agree; `make release-check VERSION=x` verifies them.

## Cut a release

`main` is **protected** — every change must land through a PR that passes the
required status checks, and that includes the version bump. (The release bot is
subject to the same rule, which is why CI no longer pushes a bump commit back to
`main`.) So the bump rides in on a **release PR**, and the git **tag** — pushed on
the already-merged commit — is what triggers the build. The `.dmg` is versioned
from the manifests in your working tree at `make bundle` time, so the bump **must**
be merged and pulled *before* you build.

```bash
# 1. Branch off an up-to-date main.
git checkout main && git pull
git checkout -b release/v0.6.2

# 2. Bump every manifest + lockfile and refresh the changelog. Commit both.
# rewrites pyproject / Cargo.toml / tauri.conf.json + lockfiles
make release VERSION=0.6.2
# prepends the v0.6.2 section to CHANGELOG.md (needs git-cliff)
make changelog VERSION=0.6.2
git add . && git commit -m "chore(release): v0.6.2"

# 3. Open the PR; merge it once the required checks are green.
git push -u origin release/v0.6.2
# then review + merge in the usual way
gh pr create --fill

# 4. Tag the merged commit and push ONLY the tag. You don't (and can't) push main
#    directly — it's protected — but the tag pushes fine and is the CI trigger.
# picks up the merged bump
git checkout main && git pull
git tag v0.6.2 && git push origin v0.6.2

# 5. CI (triggered by the tag) builds the engine wheel/sdist and creates the GitHub
#    Release. The manifests are already correct (bumped in the PR), so there is no
#    bump-back commit — nothing else lands on main.

# 6. On your Mac, build + attach the single .dmg:
# ensure the merged bump is in your tree
git pull
# freeze engine -> sidecar -> the .dmg (versioned from the manifests)
make bundle
# uploads the .dmg AND sets the release notes
make publish-orb VERSION=0.6.2
```

CI sets the build version **from the tag**, so the engine wheel/sdist are always
versioned correctly even if a manifest was missed — but `main` (and the locally
built `.dmg`) only get the bump from the PR, so don't skip steps 2–3.
`make release-check VERSION=x` confirms every manifest agrees before you open the PR.

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

1. **gate** (ubuntu) — ruff, format, mypy, pytest, plus a manifest-vs-tag check.
   The check is **non-blocking** (a warning) because CI builds with the version
   from the tag regardless — but in the PR-based flow the manifests should already
   match, so a warning here means the release PR's bump was wrong or skipped.
2. **release** (ubuntu) — sets the *build* version from the tag (`bump_version.py`),
   `uv build` makes the engine wheel/sdist, and `softprops/action-gh-release`
   creates the GitHub Release. CI does **not** commit anything back to `main`: the
   manifest bump lands via the release PR (steps 1–3 above), because branch
   protection blocks any direct push to `main` — the bot's included.

The orb `.dmg` is added by `make publish-orb` (step 6) on a Mac, which needs the
GitHub CLI (`gh auth login`) and `cargo`/`tauri-cli` installed locally.

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
