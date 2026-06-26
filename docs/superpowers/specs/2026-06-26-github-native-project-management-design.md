# GitHub-native project management — design

**Date:** 2026-06-26
**Status:** Approved (design), pending implementation
**Branch:** `chore/github-native-pm`

## Context

Planning for this project has lived in a markdown roadmap
(`docs/plans/autobot_build_roadmap.md`): phases, status checkboxes, and a "Next
directions" backlog. We have now created a GitHub Project (**Jack Assistent**,
[project #1](https://github.com/users/mdjakkariya/projects/1/views/2)) and filed
the open work as issues (#2–#8, labelled `roadmap` + `enhancement`).

We want GitHub to be the **single source of truth** for tracking, feature
requests, and project management. The repo's markdown should hold only *durable
knowledge* (how things work, how we build) — never *what's planned or its
status*.

## Goals

- Retire the markdown roadmap; tracking/planning/feature-requests live in GitHub
  Issues + Project #1.
- Preserve the roadmap's durable, non-tracking reference material.
- Make the GitHub-only workflow the path of least resistance and the documented
  rule, so it persists (especially for AI assistants, which read `CLAUDE.md`).
- Leave nothing pointing at a deleted file.

## Non-goals

- No CI guard that blocks tracking-style markdown (considered, declined — soft
  enforcement via docs + templates is enough for a solo/small project).
- No change to the design docs' content. `docs/plans/autobot_cloud_llm_plan.md`,
  `autobot_floating_orb_ui_plan.md`, and `file_access_model.md` are *design*
  records (how a shipped feature works), not tracking — they stay as-is.
- No migration of completed-phase history into docs; it remains in git history
  and the board reflects only open work.

## Decisions (from brainstorming)

1. **Reference content** → migrate the durable sections out of the roadmap into a
   new reference doc, then delete the roadmap.
2. **Design docs** → keep as reference (only the roadmap is retired).
3. **Enforcement** → three layers: `CLAUDE.md` + `CONTRIBUTING.md` rules,
   feature + task issue templates, and a PR template requiring a linked issue.
   (No CI guard.)
4. **Reference home** → `docs/architecture/design-reference.md`.
5. **Auto-add workflow** → attempt to enable the Project's built-in "auto-add
   issues" workflow so new repo issues appear on the board automatically; fall
   back to a documented manual toggle if not scriptable.

## Detailed change list

### A. Migrate reference, delete roadmap

- **Create** `docs/architecture/design-reference.md` containing the durable,
  non-tracking sections lifted from the roadmap:
  - Interface Contracts (component → input/output table)
  - Tech Stack (choice + rationale table) and the "On Rust" note
  - Hardware Tiers table + STT note
  - Reference projects to study
  - A short header explaining this is durable design reference; planning lives in
    GitHub Project #1.
- **Drop** (kept only in git history): Golden rules (redundant with `CLAUDE.md`
  "Non-negotiable constraints"), Phases 0–4 + "Shipped beyond the original
  phases", "Next directions" (now issues #2–#8), Risk-ordering cheat sheet
  (obsolete build-order).
- **Delete** `docs/plans/autobot_build_roadmap.md`.

### B. Fix inbound references to the roadmap

| File | Current reference | New target |
|------|-------------------|------------|
| `CLAUDE.md` | "Roadmap (forward plan)" pointer; `docs/ roadmap + architecture` layout line; "see the roadmap" in header | Planning & tracking rule (see C) + `design-reference.md` |
| `src/autobot/config.py:11` | "see the roadmap" (English-only constraint) | `docs/architecture/design-reference.md` |
| `src/autobot/__init__.py:6` | "See `docs/plans/autobot_build_roadmap.md`" | `docs/architecture/design-reference.md` |
| `ui/orb-shell/README.md:135` | "needs a wake model — see the roadmap" | the getting-started / README setup section (no roadmap) |
| `docs/plans/autobot_floating_orb_ui_plan.md` | two "per the roadmap" mentions | reword to reference `design-reference.md` or drop the qualifier |
| `CHANGELOG.md:35` | historical "Record next-phase plan in the roadmap" | leave unchanged (history) |

### C. `CLAUDE.md` + `CONTRIBUTING.md` rules

- **`CLAUDE.md`**: replace the roadmap pointer (intro + the `Roadmap (forward
  plan)` bullet + the `docs/ roadmap + architecture` layout line) with a concise
  **Planning & tracking** rule:

  > Planning, feature requests, and status live in **GitHub Issues + Project #1**,
  > not in markdown. Do **not** create or edit tracking markdown (roadmaps, TODO
  > docs, status lists). To propose or plan work, open an issue (feature/task
  > template); to record how something works, add to `docs/`. Durable design
  > reference: `docs/architecture/design-reference.md`.

- **`CONTRIBUTING.md`**: add a "How we track work" section — file an issue
  (feature/task template) → it lands on Project #1 → open a PR that links it with
  `Closes #NN`.

### D. Issue templates (`.github/ISSUE_TEMPLATE/`)

- **`feature_request.yml`** — fields: problem/motivation, proposed behaviour,
  which track/area, additional context. `labels: ["enhancement", "roadmap"]`,
  `title: "[feat]: "`.
- **`task.yml`** — fields: scope/checklist, acceptance criteria, related issues.
  `labels: ["roadmap"]`, `title: "[task]: "`.
- **`config.yml`** — keep `blank_issues_enabled: true`; keep the Discussions link
  for *questions only* (relabel it "Question / discussion"); feature ideas now go
  through `feature_request.yml` rather than Discussions.

### E. PR template (`.github/pull_request_template.md`)

- Sections: Summary; **Linked issue** (required — `Closes #NN`); Type of change;
  Verification checklist (`make check` passes, tests added).

### F. Project auto-add (best-effort)

- Attempt to enable Project #1's built-in "Auto-add to project" workflow via the
  GitHub API so new repo issues appear on the board automatically. If not
  scriptable with the current tooling/scopes, document the manual toggle
  (Project → ⋯ → Workflows → "Auto-add to project") in `CONTRIBUTING.md`.

## Acceptance criteria

- `docs/plans/autobot_build_roadmap.md` no longer exists; its durable reference
  is in `docs/architecture/design-reference.md`.
- `grep -rn "autobot_build_roadmap\|see the roadmap" src docs CLAUDE.md ui` returns
  no live (non-CHANGELOG) hits.
- New issue templates appear in the repo's "New issue" chooser; the feature link
  no longer routes to Discussions.
- A new PR shows the PR template with the required linked-issue section.
- `CLAUDE.md` states the GitHub-only planning rule.
- `make check` passes (no code logic changed, but references in `.py` files are
  edited).

## Out-of-scope / future

- A CI guard rejecting tracking markdown, if soft enforcement proves insufficient.
- Project custom fields (Track/Phase) — current `Status` field suffices.
