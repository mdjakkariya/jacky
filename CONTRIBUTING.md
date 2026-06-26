# Contributing to Jack

Thanks for helping out — contributions are welcome, from typo fixes to features.

## Ground rules

- Read [`CLAUDE.md`](CLAUDE.md) first — it's the source of truth for *how* this repo
  is built (architecture, conventions, the non-negotiable constraints like
  on-device-only and the permission gate). Stay within those.
- Keep it **English-only** (speech in and out) and **privacy-first**: never add a
  dependency or call that sends audio, text, or user data off the machine, except
  through the existing disclosed, opt-in exceptions.

## How we track work

All planning, feature requests, and status live in **GitHub Issues + the
[project board](https://github.com/users/mdjakkariya/projects/1)** — not in markdown
files. There is no roadmap doc; `docs/` holds only durable design reference.

- **Have an idea or hit a bug?** Open an issue — use the **Feature request**,
  **Task**, or **Bug report** template. New issues are added to the board
  automatically.
- **Picking something up?** Assign yourself the issue and move it to *In Progress*.
- **Opening a PR?** Link the issue with `Closes #NN` so it closes and moves to
  *Done* on merge.

### Title conventions

Consistent titles keep the issue list scannable and the auto-generated changelog
clean (it's built from commit subjects by git-cliff).

- **Issues** — a type prefix in brackets (the templates add it): `[feat]:`,
  `[task]:`, or `[bug]:`. Example: `[task]: Add a screenshot tool`.
- **Branches** — `<type>/<short-slug>`, e.g. `feat/screenshot-tool` or
  `docs/title-conventions`.
- **Commits & PR titles** — [Conventional Commits](https://www.conventionalcommits.org)
  (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `chore:`; `!` or a
  `BREAKING CHANGE:` footer for breaking changes). **Squash-merge** each PR with a
  Conventional Commit title, so a single clean entry lands on `main` and in the
  changelog.

## Workflow

1. **Open an issue first** (see *How we track work* above) for anything non-trivial,
   so we can agree on the approach before you spend time on it.
2. Fork, branch, and make your change with tests for any new logic.
3. **Run `make check`** (ruff lint + format, mypy strict, pytest) — it must pass.
4. Use **Conventional Commits** for commit subjects (see *Title conventions* above)
   — the changelog is generated from these.
5. Open a pull request that **links its issue** (`Closes #NN`) and fills in the PR
   template. The maintainer (@mdjakkariya) is auto-requested for review.

## Sign-off (DCO)

By contributing you certify the [Developer Certificate of Origin](https://developercertificate.org)
— i.e. you wrote the change or have the right to submit it. Add a sign-off line to
each commit:

```bash
git commit -s -m "feat: …"
```

## Licensing of contributions

This project is licensed under **Apache-2.0** (see [`LICENSE`](LICENSE)). By submitting
a contribution you agree it is provided under the same license. We don't use a CLA.
