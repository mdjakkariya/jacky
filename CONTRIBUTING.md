# Contributing to Jack

Thanks for helping out — contributions are welcome, from typo fixes to features.

## Ground rules

- Read [`CLAUDE.md`](CLAUDE.md) first — it's the source of truth for *how* this repo
  is built (architecture, conventions, the non-negotiable constraints like
  on-device-only and the permission gate). Stay within those.
- Keep it **English-only** (speech in and out) and **privacy-first**: never add a
  dependency or call that sends audio, text, or user data off the machine, except
  through the existing disclosed, opt-in exceptions.

## Workflow

1. **Open an issue first** for anything non-trivial, so we can agree on the approach
   before you spend time on it.
2. Fork, branch, and make your change with tests for any new logic.
3. **Run `make check`** (ruff lint + format, mypy strict, pytest) — it must pass.
4. Use **Conventional Commits** (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`,
   `chore:`; `!` or a `BREAKING CHANGE:` footer for breaking changes) — the changelog
   is generated from these.
5. Open a pull request. The maintainer (@mdjakkariya) is auto-requested for review.

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
