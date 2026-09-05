#!/usr/bin/env python3
"""
moodle_sync.py — pull HKU Moodle course files into a study vault's inbox.

Talks to Moodle's REST web service using a mobile-app token obtained through
HKU Portal SSO. Downloads only new or changed files, drops them in
"0 Inbox/Moodle/<COURSE>/<section>/", and writes a manifest describing each
one so the ingest workflow can file it into Materials/.

    ./moodle_sync.py handler                    # one-time, per machine — see below
    ./moodle_sync.py login                      # one-time, via HKU Portal
    ./moodle_sync.py courses --map --vault ~/…  # list enrolments, build the map
    ./moodle_sync.py sync --dry-run --vault ~/…
    ./moodle_sync.py sync --vault ~/…
    ./moodle_sync.py status --vault ~/…

Run `handler` before the first `login`. Moodle finishes its mobile login by
redirecting the browser to `moodledl://token=<base64>`; on a desktop where
nothing claims that scheme the browser answers with "No apps available" and the
token is simply lost — it never reaches the address bar, so there is nothing to
copy and paste. `handler` registers a two-line script as the owner of the
scheme, whose whole job is to write the URL to a file that `login` then reads.

Three things live in three places, deliberately:

  code    this repo, so it is versioned and testable
  state   <vault>/.moodle/state.json — it describes that vault's contents
  token   ~/.config/hku-moodle/config.json, mode 600 — a live credential does
          not belong in a notes folder or in source control

Which vault to act on comes from --vault or $STUDY_VAULT. There is no default:
guessing would mean writing an inbox into some unrelated directory.
"""

import argparse
import base64
import hashlib
import html as htmllib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WWWROOT = "https://moodle.hku.hk"
SERVICE = "moodle_mobile_app"
URLSCHEME = "moodledl"
UA = "moodle_sync.py (personal study-vault sync)"

REPO = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "hku-moodle"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hku-moodle"
CAPTURE_PATH = CACHE_DIR / "token-url"
CAPTURE_SCRIPT = REPO / "moodledl-capture.sh"
DESKTOP_ID = "hku-moodle-token.desktop"
DESKTOP_PATH = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
                / "applications" / DESKTOP_ID)

# Set by resolve_vault() for the commands that need a vault. Left as None so a
# missing --vault fails loudly instead of writing somewhere plausible-looking.
VAULT = STATE_PATH = INBOX = MANIFEST = None

SKIP_MODNAMES = {"forum", "label", "quiz", "choice", "feedback", "attendance", "lti"}
SIZE_ADOPT_FLOOR = 4096   # under this, two unrelated files sharing a byte count is plausible


# ---------------------------------------------------------------- config/state

def load_config():
    if not CONFIG_PATH.exists():
        die(f"No config at {CONFIG_PATH}. Run:  ./moodle_sync.py login")
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    CONFIG_PATH.chmod(0o600)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"files": {}, "last_sync": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_vault(arg):
    """Point the module at a study vault, from --vault or $STUDY_VAULT.

    Checked rather than guessed: every path below is a write target, and a wrong
    root would scatter an inbox and a .moodle/ directory through whatever
    directory it happened to land in.
    """
    global VAULT, STATE_PATH, INBOX, MANIFEST
    raw = arg or os.environ.get("STUDY_VAULT")
    if not raw:
        die("no vault given. Pass --vault /path/to/vault, or set STUDY_VAULT.")
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        die(f"no such directory: {vault}")
    if not (vault / "CLAUDE.md").exists():
        die(f"{vault} does not look like the study vault — no CLAUDE.md in it.")
    VAULT = vault
    STATE_PATH = VAULT / ".moodle" / "state.json"
    INBOX = VAULT / "0 Inbox" / "Moodle"
    MANIFEST = INBOX / "MANIFEST.md"


# ------------------------------------------------------------------ web service

def ws(token, function, **params):
    """Call a Moodle web service function. Returns parsed JSON."""
    flat = {}

    def flatten(prefix, value):
        if isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                flatten(f"{prefix}[{i}]", v)
        elif isinstance(value, dict):
            for k, v in value.items():
                flatten(f"{prefix}[{k}]", v)
        else:
            flat[prefix] = str(value)

    for k, v in params.items():
        flatten(k, v)
    flat.update({"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json"})

    data = urllib.parse.urlencode(flat).encode()
    req = urllib.request.Request(
        f"{WWWROOT}/webservice/rest/server.php", data=data, headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        die(f"{function}: non-JSON reply: {body[:300]}")
    if isinstance(out, dict) and ("exception" in out or "errorcode" in out):
        raise MoodleError(out.get("errorcode", "?"), out.get("message", body[:300]))
    return out


class MoodleError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(f"[{code}] {message}")


def fetch(token, fileurl):
    """Fetch a pluginfile URL with the ws token appended. Returns bytes."""
    parts = urllib.parse.urlsplit(fileurl)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["token"] = token
    url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as resp:
        ctype = resp.headers.get("Content-Type", "")
        blob = resp.read()
    if blob[:1] == b"{" and "json" in ctype:
        # Moodle returns a JSON error object rather than an HTTP error code.
        try:
            err = json.loads(blob)
            raise MoodleError(err.get("errorcode", "?"), err.get("error", "download failed"))
        except json.JSONDecodeError:
            pass
    if "html" in ctype:
        # An HTML body where a file was expected means the URL was not one the
        # token is accepted on — see embedded_files() on the /pluginfile.php/ form.
        raise MoodleError("nottoken", "got an HTML page instead of a file")
    return blob


def write_file(blob, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(dest)
    return len(blob)


def download(token, fileurl, dest):
    return write_file(fetch(token, fileurl), dest)


# --------------------------------------------------------------------- helpers

def sanitise(name, fallback="untitled"):
    name = re.sub(r"[\x00-\x1f/:\\]", " ", str(name or "")).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def visible_html(raw):
    """Drop HTML comments before anything else reads the markup.

    Lecturers hide last year's content by commenting it out rather than deleting
    it. MATH2012's "Important Messages" summary is 4,199 characters of which
    *every one* sits inside a comment: Zoom join links, a different tutor,
    different tutorial groups, "after the Chinese New Year holiday". Strip tags
    without handling comments and all of it resurfaces as though it were this
    year's arrangements — worse, `<[^>]+>` shreds any comment containing tags,
    leaking the text and leaving stray "-->" markers behind.
    """
    if not raw:
        return ""
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    return re.sub(r"<!--.*$", "", raw, flags=re.S)   # unclosed comment: hide to the end


def strip_html(text, limit=400):
    """One-line summary of an HTML blob, for manifest entries."""
    text = re.sub(r"<br\s*/?>|</p>", " ", visible_html(text), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def html_to_text(raw):
    """Readable plain text from an HTML blob, keeping its line structure."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</h[1-6]>|</tr>", "\n", visible_html(raw), flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = htmllib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l.strip() for l in text.splitlines())).strip()


EMBED_RE = re.compile(r'href="([^"]+/pluginfile\.php/[^"]+)"', re.I)


def embedded_files(raw):
    """(filename, url) for files linked from inside HTML, not posted as activities.

    MATH2012 publishes its whole reading list this way — Course Information,
    Writing Mathematics, Chapter 1 — as links in label text. Walking activity
    contents alone finds nothing, which is why that course looked empty all
    semester. The href carries the browser's /pluginfile.php/ form, which ignores
    the token and returns an HTML error page; only the web-service form accepts
    it, so rewrite the path.
    """
    out, seen = [], set()
    for url in EMBED_RE.findall(visible_html(raw)):
        url = url.split("?")[0].split("#")[0]
        if "/webservice/pluginfile.php/" not in url:
            url = url.replace("/pluginfile.php/", "/webservice/pluginfile.php/")
        name = sanitise(urllib.parse.unquote(url.rsplit("/", 1)[-1]), "")
        if name and url not in seen:
            seen.add(url)
            out.append((name, url))
    return out


def page_blocks(sections):
    """(section, kind, text) for every piece of prose written on a course page.

    Section summaries and label activities are where the things that carry a
    penalty get written — assigned reading, registration deadlines, room
    changes, who the lecturer actually is. None of it is a file, so none of it
    was being captured.
    """
    blocks = []
    for s in sections:
        sname = (s.get("name") or "").strip()
        body = html_to_text(s.get("summary"))
        if body:
            blocks.append((sname, "section text", body))
        for mod in s.get("modules", []):
            body = html_to_text(mod.get("description"))
            if not body:
                continue
            kind = mod.get("modname", "")
            title = (mod.get("name") or "").strip()
            blocks.append((sname, kind if kind == "label" else f"{kind}: {title}", body))
    return blocks


def file_key(course_id, entry):
    raw = "|".join([
        str(course_id),
        urllib.parse.urlsplit(entry.get("fileurl", "")).path,
        str(entry.get("timemodified", "")),
        str(entry.get("filesize", "")),
    ])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def materials_index():
    """Index every file under any Materials/ folder, by name+size and by size alone.

    A name match is not enough, because the vault renames on ingest (schema §2):
    Moodle's ch1.pdf is filed as "Chapter 1 - Basic Concepts (revision).pdf", and
    matching on the name would re-offer it on every sync forever. Byte size does
    survive the rename, so it is the fallback — but only where it is unambiguous,
    and only above SIZE_ADOPT_FLOOR, since short files collide by chance.
    """
    by_key, by_size = {}, {}
    for path in VAULT.glob("*/Materials/**/*"):
        if path.is_file() and not path.name.startswith("."):
            rel, size = str(path.relative_to(VAULT)), path.stat().st_size
            by_key[(path.name.lower(), size)] = rel
            by_size.setdefault(size, []).append(rel)
    return by_key, by_size


def plan_file(entry, code, section_dir, *, vault, inbox, seen_content,
              adopt_index, adopt_by_size, used_dests):
    """Decide what to do with one file entry from core_course_get_contents.

    Pure apart from probing whether a destination already exists, and it mutates
    only the three caller-owned collections passed in. Kept separate from the
    download loop so the rules below can be tested without touching Moodle —
    they are subtle, and every one of them exists because of a real failure:

      * 0-byte entries      MATH2012 posts empty index.html placeholders.
      * same name and size  CCCH9018 lists every reading twice under separate
                            activity ids. `timemodified` cannot be part of the
                            identity: it records when a copy was posted, and the
                            second copy of a reading is stamped later than the
                            first, so including it caught only 19 of 103.
      * same name, differing size
                            Two real versions of the lecture 9 slides, 3 MB
                            apart. Both are kept, and the second is renamed
                            rather than left to overwrite the first.
      * adoption by size    The vault renames on ingest (schema §2), so a name
                            match misses files it already holds: Moodle's ch1.pdf
                            is filed as "Chapter 1 - Basic Concepts (revision)".
                            Size survives renaming, but only settles it when one
                            file has that size and the file is big enough that
                            the coincidence is implausible.

    Returns (action, detail):
        "skip"  -> reason string
        "dup"   -> path the identical file is already headed for
        "adopt" -> (materials path, renamed?)
        "new"   -> destination Path
    """
    if entry.get("type") != "file" or not entry.get("fileurl"):
        return "skip", "not a file"

    fname = sanitise(entry.get("filename"), "file")
    size = int(entry.get("filesize") or 0)
    if size == 0:
        return "skip", "zero bytes"

    ident = (fname.lower(), size)
    if ident in seen_content:
        return "dup", seen_content[ident]

    hit, renamed = adopt_index.get(ident), False
    if not hit and size >= SIZE_ADOPT_FLOOR:
        same_size = adopt_by_size.get(size, [])
        if len(same_size) == 1:
            hit, renamed = same_size[0], True
    if hit:
        seen_content[ident] = hit
        return "adopt", (hit, renamed)

    dest = inbox / code / section_dir / fname
    if dest in used_dests or dest.exists():
        stem, ext, n = dest.stem, dest.suffix, 2
        while dest in used_dests or dest.exists():
            dest = dest.with_name(f"{stem} ({n}){ext}")
            n += 1
    used_dests.add(dest)
    seen_content[ident] = str(dest.relative_to(vault))
    return "new", dest


def course_folders():
    """Course code -> vault course folder name, from the folders that exist."""
    out = {}
    for path in sorted(VAULT.iterdir()):
        if path.is_dir() and (path / "Materials").is_dir():
            m = re.match(r"([A-Z]{4}\d{4})", path.name)
            if m:
                out[m.group(1)] = path.name
    return out


# -------------------------------------------------------------------- commands

def read_capture(after=None):
    """The moodledl:// URL written by the scheme handler, if it is fresh."""
    if not CAPTURE_PATH.exists():
        return None
    if after is not None and CAPTURE_PATH.stat().st_mtime + 2 < after:
        return None                       # left over from an earlier attempt
    return CAPTURE_PATH.read_text().strip() or None


def refresh_desktop_db(associate):
    apps = DESKTOP_PATH.parent
    cmds = [["update-desktop-database", str(apps)]]
    if associate:
        cmds.append(["xdg-mime", "default", DESKTOP_ID, "x-scheme-handler/moodledl"])
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=False, capture_output=True)
        except FileNotFoundError:
            print(f"note: {cmd[0]} not found; you may need to log out and back in.")


def cmd_handler(args):
    """Claim the moodledl:// scheme for this desktop user.

    Moodle ends its mobile login by redirecting to moodledl://token=<base64>.
    With no application claiming that scheme the desktop shows "No apps
    available" and the token is unreachable — the link never reaches the address
    bar, so there is nothing to copy. Claiming the scheme with a script that does
    nothing but write the URL to a file turns that dead end into the normal path.
    """
    if args.remove:
        if DESKTOP_PATH.exists():
            DESKTOP_PATH.unlink()
            print(f"Removed {DESKTOP_PATH}")
        else:
            print("Not installed; nothing to remove.")
        CAPTURE_PATH.unlink(missing_ok=True)
        refresh_desktop_db(associate=False)
        return

    if not CAPTURE_SCRIPT.exists():
        die(f"missing {CAPTURE_SCRIPT}")
    CAPTURE_SCRIPT.chmod(0o755)
    DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=HKU Moodle token capture\n"
        "Comment=Catches the moodledl:// link from Moodle's mobile login\n"
        f'Exec="{CAPTURE_SCRIPT}" %u\n'
        "Terminal=false\n"
        "NoDisplay=true\n"
        "MimeType=x-scheme-handler/moodledl;\n"
        "Categories=Utility;\n"
    )
    refresh_desktop_db(associate=True)
    print(f"Installed  {DESKTOP_PATH}")
    print(f"Handles    moodledl://  ->  {CAPTURE_SCRIPT}")
    print(f"Captures   to {CAPTURE_PATH} — mode 600, deleted once the token is saved")
    print("\nTest it without logging in:")
    print(f'  xdg-open "moodledl://token=TESTVALUE" ; cat "{CAPTURE_PATH}"')
    print("\nUndo:  ./moodle_sync.py handler --remove")
    print("Next:  ./moodle_sync.py login")


def cmd_login(args):
    passport = random.randint(1, 1000)
    launch = (
        f"{WWWROOT}/admin/tool/mobile/launch.php?"
        + urllib.parse.urlencode(
            {"service": SERVICE, "passport": passport, "urlscheme": URLSCHEME}
        )
    )
    # A capture left by an earlier attempt must not be mistaken for this one.
    CAPTURE_PATH.unlink(missing_ok=True)
    started = time.time()
    handler = DESKTOP_PATH.exists()

    print("\n1. Open this URL in your browser and log in with HKU Portal:\n")
    print("   " + launch + "\n")
    if handler:
        print(f"2. Moodle then redirects to '{URLSCHEME}://token=...'. Your desktop hands")
        print("   that link to the capture handler — approve the browser's 'Open' prompt")
        print("   if one appears. Nothing else visibly happens; that is success.\n")
        pasted = input("3. Press Enter once you have logged in (or paste the link): ").strip()
    else:
        print("2. After login the browser will try to open a link starting with")
        print(f"   '{URLSCHEME}://token=...' and fail — that failure is expected.")
        print("   Copy that whole link (address bar, or right-click > Copy link address).")
        print("\n   If instead you get an 'Open With… / No apps available' dialog, the link")
        print("   is unreachable and there is nothing to copy. Quit this, run")
        print("     ./moodle_sync.py handler")
        print("   and start again; the link will then be captured for you.\n")
        pasted = input("3. Paste it here: ").strip()

    if not pasted:
        pasted = read_capture(after=started)
        if not pasted:
            die("nothing pasted, and nothing captured. If the browser showed "
                "'No apps available', run:  ./moodle_sync.py handler")
        print(f"   Using the link captured at {CAPTURE_PATH}.")

    if "token=" not in pasted:
        die("that does not look like a token URL (no 'token=' in it)")
    encoded = pasted.split("token=", 1)[1].strip().strip("/")
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except Exception as exc:
        die(f"could not decode the token URL: {exc}")

    bits = decoded.split(":::")
    if len(bits) < 2:
        die("unexpected token format")
    signature, token = bits[0], bits[1]
    expected = hashlib.md5(f"{WWWROOT}{passport}".encode()).hexdigest()
    if signature != expected:
        print("note: passport signature did not match; verifying the token directly.")

    try:
        info = ws(token, "core_webservice_get_site_info")
    except MoodleError as exc:
        die(f"Moodle rejected the token: {exc}")

    cfg = {
        "wwwroot": WWWROOT,
        "token": token,
        "userid": info["userid"],
        "username": info.get("username"),
        "courses": {},
    }
    if CONFIG_PATH.exists():
        old = json.loads(CONFIG_PATH.read_text())
        cfg["courses"] = old.get("courses", {})
    save_config(cfg)
    CAPTURE_PATH.unlink(missing_ok=True)   # the token now lives in the config only
    print(f"\nLogged in as {info.get('fullname')} ({info.get('username')}).")
    print(f"Token saved to {CONFIG_PATH} (mode 600).")
    print("Next:  ./moodle_sync.py courses --map")


def cmd_courses(args):
    cfg = load_config()
    courses = ws(cfg["token"], "core_enrol_get_users_courses", userid=cfg["userid"])
    folders = course_folders()
    mapping = dict(cfg.get("courses", {}))

    print(f"{len(courses)} enrolled course(s):\n")
    for c in sorted(courses, key=lambda c: c.get("shortname", "")):
        cid = str(c["id"])
        # Cross-listed courses carry more than one code, and the vault's is not
        # always first: STAT2602 is "_SDST2602_STAT2602_1A_2026" on Moodle. Take
        # every code in the name and use whichever one names a course folder.
        codes = re.findall(r"[A-Z]{4}\d{4}", f"{c.get('shortname','')} {c.get('fullname','')}")
        target = mapping.get(cid) or next((folders[k] for k in codes if k in folders), None)
        flag = "->" if target else " ?"
        print(f"  [{cid:>6}] {c.get('shortname','')[:44]:<44} {flag} {target or '(unmapped)'}")
        if args.map and target:
            mapping[cid] = target

    if args.map:
        cfg["courses"] = mapping
        save_config(cfg)
        print(f"\nMapped {len(mapping)} course(s) into {CONFIG_PATH}.")
        unmapped = [c for c in courses if str(c["id"]) not in mapping]
        if unmapped:
            print("Unmapped (edit the config's \"courses\" object by hand to add):")
            for c in unmapped:
                print(f"  {c['id']}  {c.get('shortname','')}")


def cmd_sync(args):
    cfg = load_config()
    token = cfg["token"]
    mapping = cfg.get("courses", {})
    if not mapping:
        die("no courses mapped. Run:  ./moodle_sync.py courses --map")

    state = load_state()
    known = state["files"]
    adopt_index, adopt_by_size = materials_index() if not args.no_adopt else ({}, {})
    started = datetime.now(timezone.utc).astimezone()

    new_files, adopted, links, errors, dupes, notices = [], [], [], [], [], []
    used_dests, embed_fetched = set(), set()
    page_seen = state.setdefault("page_text", {})

    try:
        shortnames = {str(c["id"]): c.get("shortname", "")
                      for c in ws(token, "core_enrol_get_users_courses",
                                  userid=cfg["userid"])}
    except MoodleError:
        shortnames = {}

    for cid, folder in sorted(mapping.items(), key=lambda kv: kv[1]):
        code = re.match(r"([A-Z]{4}\d{4})", folder)
        code = code.group(1) if code else folder
        if args.course and args.course.upper() not in (code.upper(), folder.upper()):
            continue
        shortname = shortnames.get(str(cid), "")
        # Two Moodle courses can map to one folder — MATH2012 has a live _1AB and
        # a dead _1A — so the heading has to say which one this is.
        print(f"\n== {code} ({shortname or folder})")
        seen_content = {}       # per course; see plan_file() for what identity means
        try:
            sections = ws(token, "core_course_get_contents", courseid=int(cid))
        except MoodleError as exc:
            print(f"   ! skipped: {exc}")
            errors.append(f"{code}: {exc}")
            continue

        # Prose written on the course page. Reported when it first appears or
        # changes, so a later sync surfaces "read §1.1-1.4 for Monday" rather
        # than repeating the whole page every time.
        known_hashes = set(page_seen.get(str(cid), []))
        hashes_now = []
        for sec_name, kind, body in page_blocks(sections):
            h = hashlib.sha1(f"{sec_name}|{kind}|{body}".encode()).hexdigest()[:16]
            hashes_now.append(h)
            if h not in known_hashes:
                notices.append({"course": code, "section": sec_name,
                                "kind": kind, "text": body})
        if not args.dry_run:
            page_seen[str(cid)] = hashes_now
        if notices and any(n["course"] == code for n in notices):
            n = sum(1 for x in notices if x["course"] == code)
            print(f"   ~ {n} new or changed note(s) on the course page")

        for si, section in enumerate(sections):
            sname = sanitise(section.get("name") or f"Section {si}", f"Section {si}")

            # Files linked from inside HTML rather than posted as activities.
            # Size is only knowable by fetching, so these are fetched up front
            # and the bytes reused if the file turns out to be new.
            for holder in [section.get("summary")] + [
                    m_.get("description") for m_ in section.get("modules", [])]:
                for fname, furl in embedded_files(holder):
                    key = hashlib.sha1(f"embed|{cid}|{furl}".encode()).hexdigest()[:16]
                    if key in known or furl in embed_fetched:
                        continue
                    try:
                        blob = fetch(token, furl)
                    except Exception as exc:
                        print(f"   ! {fname}: {exc}")
                        errors.append(f"{code}/{fname}: {exc}")
                        embed_fetched.add(furl)
                        continue
                    embed_fetched.add(furl)
                    entry = {"type": "file", "filename": fname,
                             "filesize": len(blob), "fileurl": furl}
                    action, detail = plan_file(
                        entry, code, sanitise(f"{si:02d} {sname}"),
                        vault=VAULT, inbox=INBOX, seen_content=seen_content,
                        adopt_index=adopt_index, adopt_by_size=adopt_by_size,
                        used_dests=used_dests,
                    )
                    if action == "skip":
                        continue
                    if action == "dup":
                        known[key] = {"course": code, "filename": fname,
                                      "duplicate_of": detail, "at": started.isoformat()}
                        dupes.append({"course": code, "filename": fname, "of": detail})
                        continue
                    if action == "adopt":
                        hit, renamed = detail
                        known[key] = {"course": code, "filename": fname, "adopted": hit,
                                      "renamed": renamed, "at": started.isoformat()}
                        adopted.append({"course": code, "filename": fname,
                                        "path": hit, "renamed": renamed})
                        note = "  — filed under another name" if renamed else ""
                        print(f"   = {fname}  (already in {hit}){note}")
                        continue
                    record = {
                        "course": code, "folder": folder, "section": sname,
                        "module": "(linked in page text)", "modname": "embedded",
                        "filename": fname, "size": len(blob),
                        "dest": str(detail.relative_to(VAULT)), "desc": "",
                        "timemodified": None,
                    }
                    if args.dry_run:
                        print(f"   + {fname}  ({human(len(blob))})  [dry run, embedded]")
                        new_files.append(record)
                        continue
                    write_file(blob, detail)
                    print(f"   + {fname}  ({human(len(blob))})  [embedded]")
                    known[key] = {"course": code, "filename": fname,
                                  "dest": record["dest"], "at": started.isoformat()}
                    new_files.append(record)

            for module in section.get("modules", []):
                modname = module.get("modname", "")
                modtitle = module.get("name", "")
                if modname == "url":
                    for c in module.get("contents", []):
                        links.append({
                            "course": code, "section": sname, "module": modtitle,
                            "url": c.get("fileurl", ""),
                            "desc": strip_html(module.get("description")),
                        })
                    continue
                if modname in SKIP_MODNAMES:
                    continue

                for entry in module.get("contents", []):
                    if entry.get("type") != "file" or not entry.get("fileurl"):
                        continue
                    key = file_key(cid, entry)
                    if key in known:
                        continue

                    fname = sanitise(entry.get("filename"), "file")
                    size = int(entry.get("filesize") or 0)
                    action, detail = plan_file(
                        entry, code, sanitise(f"{si:02d} {sname}"),
                        vault=VAULT, inbox=INBOX, seen_content=seen_content,
                        adopt_index=adopt_index, adopt_by_size=adopt_by_size,
                        used_dests=used_dests,
                    )

                    if action == "skip":
                        continue
                    if action == "dup":
                        known[key] = {"course": code, "filename": fname,
                                      "duplicate_of": detail, "at": started.isoformat()}
                        dupes.append({"course": code, "filename": fname, "of": detail})
                        continue
                    if action == "adopt":
                        hit, renamed = detail
                        known[key] = {"course": code, "filename": fname, "adopted": hit,
                                      "renamed": renamed, "at": started.isoformat()}
                        adopted.append({"course": code, "filename": fname,
                                        "path": hit, "renamed": renamed})
                        note = "  — filed under another name" if renamed else ""
                        print(f"   = {fname}  (already in {hit}){note}")
                        continue

                    dest = detail
                    record = {
                        "course": code, "folder": folder, "section": sname,
                        "module": modtitle, "modname": modname, "filename": fname,
                        "size": size, "dest": str(dest.relative_to(VAULT)),
                        "desc": strip_html(module.get("description")),
                        "timemodified": entry.get("timemodified"),
                    }
                    if args.dry_run:
                        print(f"   + {fname}  ({human(size)})  [dry run]")
                        new_files.append(record)
                        continue
                    try:
                        got = download(token, entry["fileurl"], dest)
                    except Exception as exc:
                        print(f"   ! {fname}: {exc}")
                        errors.append(f"{code}/{fname}: {exc}")
                        continue
                    print(f"   + {fname}  ({human(got)})")
                    known[key] = {"course": code, "filename": fname,
                                  "dest": record["dest"], "at": started.isoformat()}
                    new_files.append(record)
                    time.sleep(0.2)

    if args.dry_run:
        print(f"\nDry run: {len(new_files)} new file(s), {len(adopted)} already in "
              f"Materials, {len(dupes)} duplicate upload(s) skipped, "
              f"{len(notices)} page note(s) new or changed.")
        return

    state["last_sync"] = started.isoformat()
    save_state(state)
    if new_files or links or notices:
        write_manifest(started, new_files, adopted, links, errors, dupes, notices)
    print(f"\n{len(new_files)} downloaded, {len(adopted)} adopted, "
          f"{len(dupes)} duplicate(s) skipped, {len(notices)} page note(s), "
          f"{len(errors)} error(s).")
    if new_files:
        print(f"Manifest: {MANIFEST.relative_to(VAULT)}")
        print('Now tell Claude: "ingest the Moodle inbox".')


def write_manifest(started, new_files, adopted, links, errors, dupes=(), notices=()):
    INBOX.mkdir(parents=True, exist_ok=True)
    day = started.strftime("%Y-%m-%d %H:%M")
    out = [f"\n## Sync {day}\n"]

    if new_files:
        out.append("### Downloaded — needs filing\n")
        by_course = {}
        for f in new_files:
            by_course.setdefault(f["course"], []).append(f)
        for code, files in sorted(by_course.items()):
            out.append(f"**{code}**\n")
            for f in files:
                out.append(f"- `{f['dest']}`")
                out.append(f"  - Moodle section: {f['section']} · activity: {f['module']} ({f['modname']}) · {human(f['size'])}")
                if f["desc"]:
                    out.append(f"  - Description: {f['desc']}")
                out.append(f"  - Suggested target: `{f['folder']}/Materials/` — topic folder TBD")
            out.append("")

    if notices:
        out.append("### Written on the course page — new or changed\n")
        out.append("Section text and labels: not files, so nothing to file, but this is "
                   "where assigned reading, deadlines and room changes get announced. "
                   "Hidden (commented-out) blocks are excluded.\n")
        by_course = {}
        for n in notices:
            by_course.setdefault(n["course"], []).append(n)
        for code, items in sorted(by_course.items()):
            out.append(f"**{code}**\n")
            for n in items:
                head = f"{n['section']} · {n['kind']}" if n["section"] else n["kind"]
                out.append(f"- *{head}*")
                for line in n["text"].splitlines():
                    out.append(f"  > {line}" if line.strip() else "  >")
            out.append("")

    if links:
        out.append("### Links — not downloadable (Panopto, external readings)\n")
        for l in links:
            out.append(f"- {l['course']} · {l['section']} · [{l['module']}]({l['url']})")
            if l["desc"]:
                out.append(f"  - {l['desc']}")
        out.append("")

    if adopted:
        out.append(f"### Already in Materials — skipped ({len(adopted)})\n")
        for a in adopted:
            note = " *(matched by size — filed under another name)*" if a.get("renamed") else ""
            out.append(f"- {a['course']} · {a['filename']} → `{a['path']}`{note}")
        out.append("")

    if dupes:
        out.append(f"### Duplicate uploads on Moodle — skipped ({len(dupes)})\n")
        out.append("Byte-identical files posted more than once in the same course. "
                   "One copy was taken; these are the copies not fetched.\n")
        for d in dupes:
            out.append(f"- {d['course']} · {d['filename']} → same file as `{d['of']}`")
        out.append("")

    if errors:
        out.append("### Errors\n")
        for e in errors:
            out.append(f"- {e}")
        out.append("")

    header = ""
    if not MANIFEST.exists():
        header = (
            "---\ntype: index\ncourse: cross\nstatus: solid\n"
            f"updated: {started.strftime('%Y-%m-%d')}\n---\n\n"
            "# Moodle sync manifest\n\n"
            "Appended by `moodle_sync.py` (repo: ~/Desktop/hku-moodle-sync). Each "
            "entry is a file pulled from "
            "Moodle and dropped in this inbox, with the section and activity it came "
            "from — enough context to file it into the right `Materials/` topic "
            "folder. Delete a sync block once everything under it is filed.\n"
        )
    with MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(header + "\n".join(out) + "\n")


def cmd_status(args):
    cfg = load_config()
    state = load_state()
    print(f"Account:    {cfg.get('username')} (userid {cfg.get('userid')})")
    print(f"Courses:    {len(cfg.get('courses', {}))} mapped")
    print(f"Last sync:  {state.get('last_sync') or 'never'}")
    print(f"Tracked:    {len(state.get('files', {}))} files")
    pending = [p for p in INBOX.rglob("*") if p.is_file() and p.name != "MANIFEST.md"] \
        if INBOX.exists() else []
    print(f"Inbox:      {len(pending)} file(s) awaiting filing")
    for p in pending[:20]:
        print(f"  - {p.relative_to(VAULT)}")
    if len(pending) > 20:
        print(f"  … and {len(pending) - 20} more")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="obtain a web-service token via HKU Portal SSO")

    p = sub.add_parser("handler", help="claim moodledl:// so the token can be captured")
    p.add_argument("--remove", action="store_true", help="unregister it again")

    # The vault-aware commands. `login` and `handler` touch only ~/.config and
    # the desktop, so they take no --vault and must not require one.
    needs_vault = {}

    p = sub.add_parser("courses", help="list enrolled courses")
    p.add_argument("--map", action="store_true", help="save the auto-detected course mapping")
    needs_vault["courses"] = p

    p = sub.add_parser("sync", help="download new and changed course files")
    p.add_argument("--dry-run", action="store_true", help="show what would be fetched")
    p.add_argument("--course", help="limit to one course code, e.g. LLAW2012")
    p.add_argument("--no-adopt", action="store_true",
                   help="do not skip files already present in Materials/")
    needs_vault["sync"] = p

    needs_vault["status"] = sub.add_parser(
        "status", help="show sync state and pending inbox files")

    for p in needs_vault.values():
        p.add_argument("--vault", metavar="PATH",
                       help="the study vault to act on; defaults to $STUDY_VAULT")

    args = ap.parse_args()
    if args.cmd in needs_vault:
        resolve_vault(args.vault)
    try:
        {"login": cmd_login, "handler": cmd_handler, "courses": cmd_courses,
         "sync": cmd_sync, "status": cmd_status}[args.cmd](args)
    except MoodleError as exc:
        if exc.code in ("invalidtoken", "accessexception"):
            die(f"{exc}\nToken expired or revoked. Run:  ./moodle_sync.py login")
        die(str(exc))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
