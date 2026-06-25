# File access model — central, grant-based

How Jack reads and (later) writes files anywhere on the Mac **safely**, through one
central policy that every file tool consults — not a per-tool jail. This is the
foundation for the code-capability work (reading/searching projects, then writing
and editing them).

## Principles (from prior art)

- **Deny by default + an allowlist of roots.** A path is permitted only if it
  resolves inside a *granted root*. Folder grants are recursive. Path checks resolve
  symlinks and collapse `..` so nothing escapes. (MCP filesystem server.)
- **Grant once, persist, tied to explicit user intent.** The user approves a folder;
  the grant survives restarts. (Mirrors macOS security-scoped bookmarks.)
- **Per-operation modes + least privilege + revocable.** A grant is `read` or
  `read-write`; writing asks to upgrade. Grants are listed and revocable in Settings,
  to avoid "lingering authority". (Claude Code allow/ask/deny; capability research.)

## Design

`AccessPolicy` (`tools/access.py`) replaces the single-root `Sandbox` as the one
component every file tool calls:

- **State:** a set of granted roots, each with a mode (`READ` / `WRITE`; write
  implies read). The workspace (`~/.autobot/workspace`) is always granted read-write.
- **`check(path, write=False) -> Path`:** resolve the path; if it hits the built-in
  **secret denylist** (`~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains, `.env`, key files)
  raise `AccessDenied` *even inside a granted root*; if it's inside a granted root
  with sufficient mode, return the resolved path; otherwise raise `NeedsAccess(folder,
  mode)`.
- **Grants:** `grant(path, write)`, `revoke(path)`, `grants()`. Persisted as
  `~/.autobot/access.json` (`{path, mode}` list) — kept out of `settings.json`
  (tunables) and audited via `audit.db`. Thread-safe (engine + daemon threads).

**Grant-on-first-use:** a tool calls `check`; on `NeedsAccess` it asks the user
(the existing confirm-card mechanism) "Give Jack read access to `<folder>`?" —
approve → `grant` + retry; a write to a read-only root prompts to upgrade. Folders
can also be pre-granted from **Settings → Folders & access** (native picker;
list/revoke).

**Risk stays orthogonal.** The `PermissionGate` keeps classifying each op
(read = READ_ONLY, write = WRITE, delete = DESTRUCTIVE). `AccessPolicy` adds *scope*
(which folders); the gate keeps *risk* (how dangerous). New tools just call
`policy.check(path, write=…)` — no new jail.

## Caveats

- **macOS:** while Jack ships unsigned/non-sandboxed it already has the launching
  user's file access, so `AccessPolicy` is *our* guardrail, not the OS's. True
  OS-enforced persistence (security-scoped bookmarks) lands with notarization (the
  deferred "Distribution & trust" track).
- **Cloud mode:** reading file *contents into the model* sends them to Anthropic in
  cloud mode. So distinguish "read into model" (summarize a file) from "copy to
  clipboard" (content never enters the model — safe even in cloud mode). The denylist
  + grants protect the former.

## Phasing

1. `AccessPolicy` core + persistence (this is the keystone).
2. Tools consult it: `read_file_text`, `copy_file_to_clipboard` (read), `write_file`,
   `edit_file` (write); migrate `filesystem.py` off the bare `Sandbox`.
3. Grant-on-first-use prompt + Settings "Folders & access" panel.
4. Later: macOS security-scoped bookmarks when notarized.
