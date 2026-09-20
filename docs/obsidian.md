# Obsidian project sync

`scripts/obsidian_sync.py` mirrors Sam's **project state** into a local Obsidian
vault so the roadmap, phases and security principles are easy to browse. It is a
documentation tool. It is **not** a Memory export, not a chat log and not part of
Sam's runtime.

## Guarantees

* **Local only.** No network access of any kind: no Claude, Gemini, OpenAI,
  Grok, MCP, GitHub or web call. Tests run it with sockets disabled.
* **Manual and one-shot.** You run it; it does its work and exits. There is no
  daemon, file watcher, cron/launchd job, background service, listener or
  continuous sync.
* **Never touches Git.** It makes read-only Git queries (`rev-parse`, `ls-files`,
  `diff --name-only`, `check-ignore`) through a fixed `/usr/bin/git` with no
  shell. It never adds, commits, pushes or changes configuration.
* **The vault is never committed.** `Obsidian/` (and Obsidian's own
  `.obsidian/` settings folders) are in `.gitignore`. Before
  writing, the tool refuses to run if any `Obsidian/` path is tracked or staged,
  if the exact ignore rule is missing, or if Git does not ignore the destination.
* **Nothing is deleted.** It creates and updates its own notes only.

## Usage

```
python scripts/obsidian_sync.py --dry-run                 # preview, writes nothing
python scripts/obsidian_sync.py                           # sync the default vault
python scripts/obsidian_sync.py --vault /path/to/vault    # sync another vault
python scripts/obsidian_sync.py --verification report.json
```

* **Choosing a vault.** With no `--vault`, the repository's own ignored
  `Obsidian/` directory is used. `--vault PATH` selects any other directory. A
  vault inside the repository must be exactly `Obsidian/`. The path must not be
  a symlink (or contain one), and it must be a real Obsidian vault: it contains a
  `.obsidian` directory, or it is an open vault in Obsidian's own registry.
* **`--dry-run`** performs all rendering and validation (secret checks, vault
  and Git safety) and reports what would be created, updated or left unchanged.
  It writes nothing, not even directories.
* Output lists note names and counts only. Note content and rejected material
  are never printed.
* **`--verification FILE`** (optional) shows an operator-written test summary on
  the Dashboard. It must match the current Git HEAD and a fixed schema. The sync
  itself never runs tests and never approves a phase.

Run it from the repository root with the project's Python environment. Errors
exit with status 2 and a fixed, content-free message.

## What is written

Notes live under `<vault>/Sam/`, generated only from validated
`PROJECT_SPEC.json` fields and Git HEAD/branch:

| Note | Source |
|---|---|
| `Dashboard.md` | project name/version, current phase, approved count, HEAD |
| `Roadmap.md` | every phase and its status |
| `Phases/Phase NN - Name.md` | scope, notes, rules, and links to repository docs |
| `Security/Security Principles.md` | security principles, never-allowed list, risk levels |
| `Engineering/Engineering Overview.md` | architecture layers/flow, development rules, technology |
| `Integrations/MCP Integrations.md` | MCP integration purposes and permission lists |

A section with no trusted source is **not generated**: nothing is invented for
ADRs, Professional, Career, Daily or Activity. Create those folders and notes
yourself; the tool will never touch them.

## Managed blocks

Each generated note is one managed block:

```
<!-- SAM:AUTO:START -->
... Sam-generated text ...
<!-- SAM:AUTO:END -->
```

* Only the text between the markers is replaced. **Everything you write outside
  the markers is preserved byte for byte**, above and below.
* The output is deterministic (no timestamps), so running it twice changes
  nothing.
* It fails closed, without touching the note, if the markers are missing,
  reversed, duplicated or unbalanced, or if a same-named note exists that has no
  markers at all (your own note is "left intact").
* Writes are atomic per note, use a private temporary file, and abort if the
  note changed while the sync was running.

## Privacy and security

All notes are rendered and validated **before any note is written**, so a single
rejection publishes nothing. The sync rejects, and never echoes, content that
looks like:

* API keys, access/refresh/ID tokens, passwords, credentials, private keys,
  authorization headers, session tokens, GitHub/Slack/Google/JWT-style tokens;
* voice-identity material: biometric or speaker embeddings, voice templates,
  owner-proof and step-up/bridge secrets, or a long run of decimals that looks
  like an embedding vector;
* raw recordings or audio file paths (`.wav`, `.m4a`, `.mp3`, ...);
* control characters, oversize content, or marker injection.

It uses Sam's existing secret detector plus stricter extra rules. The check runs
on the exact text and on a Unicode-normalized view, and it also scans your own
text in a note it is about to rewrite. The detector targets secret-shaped
*values*, not vocabulary: prose that merely mentions "credential isolation" is
fine.

**Intentionally not synchronized:** Memory contents, Knowledge documents,
conversations and transcripts, private or intimate content, audio, biometric
data, credentials, application source, configuration files and `.env`. It only
reads `PROJECT_SPEC.json`, Git HEAD/branch, and the existence of a few
repository docs.

## Filesystem safety

Paths are walked component by component without following symlinks. A symlinked
vault, `Sam/` directory, subdirectory or note, a hard-linked note, a non-regular
file, or a path that would leave the vault is rejected. Generated note paths are
fixed, short and validated, so nothing can escape `<vault>/Sam/`.

## Limits

* It reflects `PROJECT_SPEC.json` and Git at the moment you run it; the Dashboard
  shows the HEAD it saw, so it is stale until you sync again.
* It cannot detect every possible secret. It is a safety net for a
  project-state mirror, not a substitute for keeping private content out of
  `PROJECT_SPEC.json`.
* Obsidian's own sync or backup features are outside this tool. Keep the vault
  out of any repository you push.
