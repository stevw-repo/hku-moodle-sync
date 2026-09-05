# hku-moodle-sync

Pulls HKU Moodle course files into a study vault's inbox, so course materials
arrive by themselves instead of being downloaded by hand a file at a time.

It talks to Moodle's REST web service with a mobile-app token, downloads only
what is new, and drops each file in `0 Inbox/Moodle/<COURSE>/<section>/` with a
manifest recording where it came from — enough context to file it into the right
`Materials/` topic folder afterwards. It deliberately stops there: retrieval is
mechanical and safe to automate, filing is not.

## Three things in three places

| | Where | Why |
|---|---|---|
| Code | this repo | versioned, testable, reusable across vaults |
| State | `<vault>/.moodle/state.json` | it describes *that* vault's contents |
| Token | `~/.config/hku-moodle/config.json`, mode 600 | a live credential belongs in neither a notes folder nor source control |

The vault is named per-run with `--vault`, or `$STUDY_VAULT`. There is no
default: guessing would mean writing an inbox into an unrelated directory.

## Setup

```sh
./moodle_sync.py handler          # once per machine — see below
./moodle_sync.py login            # once, via HKU Portal SSO
./moodle_sync.py courses --map --vault ~/path/to/vault
./moodle_sync.py sync --dry-run --vault ~/path/to/vault
./moodle_sync.py sync --vault ~/path/to/vault
```

### Why `handler` has to come first

Moodle finishes its mobile login by redirecting the browser to
`moodledl://token=<base64>`. On a desktop where nothing claims that scheme, the
browser answers with **"No apps available"** and the token is simply gone — it
never reaches the address bar, so there is nothing to copy and paste. The
instruction to "copy the link" is impossible to follow.

`handler` registers `moodledl-capture.sh` as the owner of the scheme. That
script does one thing: write the URL to `~/.cache/hku-moodle/token-url`, mode
600, which `login` then reads and deletes once the token is saved.

Verify it without logging in:

```sh
xdg-open "moodledl://token=TESTVALUE" ; cat ~/.cache/hku-moodle/token-url
```

Undo with `./moodle_sync.py handler --remove`. The registration stores this
repo's absolute path, so **re-run `handler` if you move the repo**, or login
silently returns to the dead end.

If you would rather not register anything, the token is also readable from
DevTools → Network → the `launch.php` request → Response Headers → `Location:`.
`login` still accepts a pasted link.

## What it assumes about the vault

Written against an Obsidian study vault with one folder per course
(`LLAW2012 Commercial law`), each holding a `Materials/` directory. Course
mapping is by course code found in the Moodle shortname — including
cross-listed names, where the vault's code is not necessarily first:
`_SDST2602_STAT2602_1A_2026` maps to `STAT2602 …` on its second code, not its
first.

`resolve_vault()` refuses a directory with no `CLAUDE.md` in it, as a guard
against a mistyped `--vault`.

## The rules that stop it making a mess

Each of these exists because of a real failure against HKU Moodle, and each is
covered in `tests/test_plan_file.py`. The first version reported 249 files to
download when the true figure was 139, and the output looked entirely plausible
either way — which is why they are tested.

- **0-byte entries are skipped.** MATH2012 posts empty `index.html` placeholders.
- **Same name and size is a duplicate.** CCCH9018 lists every reading twice
  under separate activity ids. `timemodified` cannot be part of the identity: it
  records when a *copy was posted*, not what is in it, so including it caught
  only 19 of 103 duplicated names.
- **Same name, different size means two real files.** Two versions of one
  lecture's slides, 3 MB apart. Both are kept; the second is written as
  `… (2).pdf` rather than overwriting the first.
- **Adoption by size.** A vault that renames on ingest defeats name matching —
  Moodle's `ch1.pdf` gets filed as `Chapter 1 - Basic Concepts (revision).pdf`,
  and matching on name would re-offer it on every sync forever. Byte size
  survives renaming, so it is the fallback, but only when exactly one file in
  `Materials/` has that size and the file is at least `SIZE_ADOPT_FLOOR` bytes
  (4 KB) — below that a shared byte count is plausible coincidence.

Adoption skips a download; it never deletes or modifies anything. If it ever
adopts wrongly, `--no-adopt` re-fetches.

## Not downloaded

`url` activities (Panopto recordings, external readings) cannot be fetched — they
are listed in the manifest as links instead. Forums, labels, quizzes, choices,
feedback, attendance and LTI activities are skipped entirely.

## Tests

```sh
python3 -m pytest tests/ -q
```

No network and no fixtures beyond `tmp_path`: `plan_file()` is pure apart from
probing whether a destination already exists.

## Requirements

Python 3.9+ (uses `Path.unlink(missing_ok=True)`), standard library only.
`pytest` for the tests. Linux desktop with `xdg-mime` / `update-desktop-database`
for the `moodledl://` handler; everything else is portable.
