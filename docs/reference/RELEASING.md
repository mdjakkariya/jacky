# Releasing Jack

Jack ships on **two independent release tracks** — the CLI/engine and the macOS
**orb** app version, tag, build, and changelog on their own schedules. Shipping a
CLI patch never forces an orb release, and vice versa. Both tracks are still
**PR-bumped, tag-triggered**: the version bump lands on `main` through a normal
**PR** (branch protection blocks direct pushes — see below), and pushing the
resulting tag is what triggers the build.

## Two release tracks

|                  | **CLI / engine**                                          | **Orb (macOS app)**                                    |
| ---------------- | ----------------------------------------------------------- | -------------------------------------------------------- |
| Version files    | `pyproject.toml`, `src/autobot/__init__.py`                 | `ui/orb-shell/src-tauri/{Cargo.toml,Cargo.lock,tauri.conf.json}` |
| Bump command      | `make release-cli VERSION=x`                                 | `make release-orb VERSION=x`                              |
| Tag              | `vX.Y.Z`                                                     | `orb-vX.Y.Z`                                              |
| Build             | **CI** (`.github/workflows/release.yml`) — engine wheel/sdist + 5 `jack` binaries + PyPI publish (`jacky`) | **Manual, on a Mac** (`make bundle` + `make publish-orb`) |
| Changelog config | [`cliff.cli.toml`](../../cliff.cli.toml)                     | [`cliff.orb.toml`](../../cliff.orb.toml)                  |
| Changelog file   | [`CHANGELOG-cli.md`](../../CHANGELOG-cli.md)                 | [`CHANGELOG-orb.md`](../../CHANGELOG-orb.md)               |
| Changelog command | `make changelog-cli VERSION=x`                              | `make changelog-orb VERSION=x`                             |

Both tracks share one thing: `main` is **protected** — every change (including the
version bump) must land through a PR that passes the required status checks. The
release bot is subject to the same rule, which is why neither CI job pushes a bump
commit back to `main`; the bump rides in on a release PR, and the git **tag** —
pushed on the already-merged commit — is what triggers the build.

## CLI/engine release

This is the **automated** path: pushing a `vX.Y.Z` tag builds the Python engine
(wheel + sdist) *and* the `jack` CLI binaries for every platform we publish,
attaches them all to one GitHub Release, and publishes the engine to **PyPI** as
[`jacky`](https://pypi.org/project/jacky/) (the repo's name — `autobot` and `jack`
are both squatted on PyPI by abandoned 2014 packages; the import package and the
`jack`/`autobot` commands keep their names).

```bash
git checkout -b release/v0.8.1 main
make release-cli VERSION=0.8.1
make changelog-cli VERSION=0.8.1
git commit -am "chore(release): v0.8.1" && gh pr create --fill   # merge when green
git checkout main && git pull
git tag v0.8.1 && git push origin v0.8.1     # CI builds wheel + 5 jack binaries
```

`make release-cli` rewrites `pyproject.toml` and `src/autobot/__init__.py` (the
version compiled into the frozen `jack` binary); `make release-check-cli VERSION=x`
verifies they agree before you open the PR. `make changelog-cli` prepends the
`vX.Y.Z` section to `CHANGELOG-cli.md` via `git-cliff --config cliff.cli.toml`
(needs `git-cliff`, `brew install git-cliff`).

### What CI produces

Pushing the tag triggers `.github/workflows/release.yml`, which:

1. **gate** — the same quality checks as CI (ruff, format, mypy, pytest), plus a
   non-blocking manifest-vs-tag check. It's a warning, not a failure, because the
   `release`/`binaries` jobs build with the version taken **from the tag**
   regardless — but a warning here means the release PR's bump was wrong or
   skipped, so treat it as a signal to double-check.
2. **release** — sets the build version from the tag (`scripts/bump_version.py cli`),
   runs `uv build`, creates the GitHub Release for the tag, and uploads the dist as
   a workflow artifact for the publish job.
3. **binaries** — after `release`, builds and attaches the frozen `jack` CLI
   binary for every published platform, in parallel (`fail-fast: false`, so one
   platform breaking doesn't cancel the rest).
4. **publish-pypi** — after `release`, publishes the wheel + sdist to PyPI as
   `jacky` via **Trusted Publishing** (OIDC): no API-token secret exists — PyPI
   verifies a short-lived token minted for this repo + `release.yml` + the `pypi`
   GitHub environment. That environment has a **required-reviewer rule, so the job
   waits until you approve it in the run's page** (Actions → the release run →
   *Review deployments*). PyPI files are immutable — a published version can never
   be re-uploaded, so fixing a bad release means a new patch tag (re-running a
   *failed* publish job is fine).

What ends up attached to the `v0.8.1` Release:

- `jacky-*.whl` / `jacky-*.tar.gz` — the Python engine (wheel + sdist; the same
  files that land on PyPI).
- `jack-0.8.1-<os>-<arch>.(tar.gz|zip)` + a matching `.sha256` for each of:
  macOS arm64, macOS x64, Linux x64, Linux arm64, Windows x64.

`install.sh`/`install.ps1` and `jack update` both consume those assets directly —
the `jack-<version>-<os>-<arch>` naming (and the `.sha256` format: a bare lowercase
hex digest, no filename) is a contract with the client, defined once in
`autobot.update.asset_name` and matched by the workflow's packaging step. Changing
the naming means updating that function, `release.yml`, `install.sh`, and
`install.ps1` together.

**Note:** the `binaries` job's macOS runner labels (`macos-15`, `macos-15-intel`)
are time-sensitive — GitHub periodically retires older hosted-runner images
(`macos-13` is already gone, `macos-14` is deprecating). **Re-verify the labels
against GitHub's current hosted-runner list before cutting a CLI release** — a
stale label fails that leg silently (the matrix is `fail-fast: false`), so confirm
all five binary assets actually attached to the first tag of a release.

## Orb release

This is the **manual** path — the macOS orb app is built locally on a Mac and its
`.dmg` uploaded by hand, because macOS CI runner minutes are ~10x the cost of the
CLI/engine's Linux build.

```bash
git checkout -b release/orb-v0.3.0 main
make release-orb VERSION=0.3.0
make changelog-orb VERSION=0.3.0
git commit -am "chore(release): orb-v0.3.0" && gh pr create --fill   # merge when green
git checkout main && git pull
git tag orb-v0.3.0 && git push origin orb-v0.3.0   # inert in CI
make bundle && make publish-orb VERSION=0.3.0       # on a Mac; uploads the .dmg to orb-v0.3.0
```

`make release-orb` rewrites the three `src-tauri` manifests (`Cargo.toml`,
`Cargo.lock`, `tauri.conf.json`); `make release-check-orb VERSION=x` verifies them.
`make changelog-orb` prepends the `orb-vX.Y.Z` section to `CHANGELOG-orb.md` via
`git-cliff --config cliff.orb.toml`.

Pushing an `orb-v*` tag is **inert in CI** — `release.yml` only triggers on `v*.*.*`
tags, so the orb tag exists purely as a marker on the commit the `.dmg` was built
from; it does not kick off a workflow. The build itself is local:

- `make bundle` freezes the engine (`make freeze`), builds the native syscap
  sidecar, and runs `cargo tauri build`, producing one `.dmg` that contains both.
  See [`PACKAGING.md`](PACKAGING.md).
- `make publish-orb VERSION=0.3.0` finds that `.dmg`, generates release notes from
  `cliff.orb.toml` (falling back to a plain "dev preview" note if `git-cliff` isn't
  installed), creates or updates the `orb-v0.3.0` GitHub Release, and uploads the
  `.dmg` to it.

**Important:** the orb bundles whatever version of the **engine** is in your
working tree at `make bundle` time — there is no separate "orb's engine version."
If you've pulled a newer CLI/engine release since your last orb build, that newer
engine ships inside the next orb `.dmg` too. Note which engine version (`v*` tag or
commit) went into the bundle in the release notes so users and future debugging
know what's actually inside a given `orb-v*` build.

## Versioning

Semantic Versioning (`MAJOR.MINOR.PATCH`) on each track independently. The git tag
(`vX.Y.Z` for the CLI/engine, `orb-vX.Y.Z` for the orb) is the source of truth for
that track; `scripts/bump_version.py` keeps each track's manifests in lockstep.

## Signing (unsigned dev preview)

The `.dmg` is **unsigned** — fine for a dev preview. macOS Gatekeeper warns the
first time; users right-click the app → **Open** → **Open**. To distribute without
the warning later, add an Apple Developer ID cert + notarization secrets and pass
them to `tauri-action` (`APPLE_CERTIFICATE`, `APPLE_ID`, `APPLE_PASSWORD`,
`APPLE_TEAM_ID`).

The `jack` CLI binaries are similarly unsigned; `install.sh`/`install.ps1` and
`jack update` verify them by SHA-256 against the release asset, not by code
signature.

## What a developer needs to run a release build

The engine isn't fully standalone — it still needs the runtime pieces:

- **Ollama** running locally (default), *or* an Anthropic API key (cloud mode).
- The STT model (faster-whisper downloads on first run; whisper.cpp via the
  `whispercpp` extra) and a Piper voice for TTS.
- macOS **Microphone** permission, and **Automation** (Finder) permission for
  `empty_trash`.

See the README "Try a release" section for the user-facing steps.
