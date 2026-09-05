"""Tests for plan_file() — the per-file decision rules.

Every case here is a real failure seen against HKU Moodle on 2026-09-04/05, not
a hypothetical. The first version of this logic reported 249 files to download
when the true figure was 139, and the output looked entirely plausible either
way — which is the reason these rules are tested at all.

    python3 -m pytest tests/ -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import moodle_sync as m


def entry(filename, size, url="https://moodle.hku.hk/webservice/pluginfile.php/1/x"):
    return {"type": "file", "filename": filename, "filesize": size, "fileurl": url}


@pytest.fixture
def ctx(tmp_path):
    """A caller context with nothing already known."""
    return {
        "vault": tmp_path,
        "inbox": tmp_path / "0 Inbox" / "Moodle",
        "seen_content": {},
        "adopt_index": {},
        "adopt_by_size": {},
        "used_dests": set(),
    }


def plan(e, ctx, code="LLAW2012", section="06 Lecture 1"):
    return m.plan_file(e, code, section, **ctx)


# --------------------------------------------------------------------- skipping

def test_zero_byte_files_are_skipped(ctx):
    """MATH2012 posts empty index.html placeholders."""
    assert plan(entry("index.html", 0), ctx) == ("skip", "zero bytes")


def test_non_file_entries_are_skipped(ctx):
    assert plan({"type": "url", "filename": "x", "filesize": 10}, ctx)[0] == "skip"


# ------------------------------------------------------------------ duplicates

def test_identical_name_and_size_is_a_duplicate(ctx):
    """CCCH9018 lists every reading twice under separate activity ids."""
    first = plan(entry("Filial Piety.pdf", 80_123), ctx)
    assert first[0] == "new"

    second = plan(entry("Filial Piety.pdf", 80_123), ctx)
    assert second[0] == "dup"
    assert second[1] == str(first[1].relative_to(ctx["vault"]))


def test_duplicate_detection_ignores_timemodified(ctx):
    """The identity must not include mtime.

    Moodle stamps timemodified when a copy is *posted*, so the second upload of
    a reading is stamped later than the first. Including it in the identity
    caught only 19 of the 103 duplicated names in CCCH9018.
    """
    a = entry("Reading.pdf", 85_291)
    b = entry("Reading.pdf", 85_291)
    a["timemodified"], b["timemodified"] = 1788250158, 1788175850

    assert plan(a, ctx)[0] == "new"
    assert plan(b, ctx)[0] == "dup"


def test_same_name_different_size_keeps_both(ctx):
    """Two real versions of the CCCH9018 lecture 9 slides, 3 MB apart."""
    first = plan(entry("09 - Buddhism and Chinese Arts.pdf", 11_650_636), ctx)
    second = plan(entry("09 - Buddhism and Chinese Arts.pdf", 8_762_612), ctx)

    assert first[0] == "new" and second[0] == "new"
    assert first[1] != second[1], "the second must not overwrite the first"
    assert second[1].name == "09 - Buddhism and Chinese Arts (2).pdf"


def test_duplicates_are_scoped_per_call_site(ctx, tmp_path):
    """seen_content is owned by the caller, which resets it per course."""
    plan(entry("Course outline.pdf", 5_000), ctx)
    ctx["seen_content"] = {}          # as cmd_sync does when the course changes
    assert plan(entry("Course outline.pdf", 5_000), ctx, code="LLAW2018")[0] == "new"


# -------------------------------------------------------------------- adoption

def test_exact_name_and_size_is_adopted(ctx):
    ctx["adopt_index"][("re gatecoin.pdf", 900_000)] = "LLAW2012/Materials/Re Gatecoin.pdf"
    action, (path, renamed) = plan(entry("Re Gatecoin.pdf", 900_000), ctx)
    assert action == "adopt"
    assert path == "LLAW2012/Materials/Re Gatecoin.pdf"
    assert renamed is False


def test_renamed_file_is_adopted_by_size(ctx):
    """The vault renames on ingest, so name matching alone re-offers files forever.

    Moodle's ch1.pdf is filed as "Chapter 1 - Basic Concepts (revision).pdf".
    """
    filed = "STAT2602/Materials/0.5 Revision/Chapter 1 - Basic Concepts (revision).pdf"
    ctx["adopt_by_size"][493_922] = [filed]

    action, (path, renamed) = plan(entry("ch1.pdf", 493_922), ctx)
    assert action == "adopt"
    assert path == filed
    assert renamed is True, "must be reported as a rename, not a plain match"


def test_ambiguous_size_is_not_adopted(ctx):
    """Two Materials files of the same size settle nothing — download it."""
    ctx["adopt_by_size"][493_922] = ["A/Materials/one.pdf", "B/Materials/two.pdf"]
    assert plan(entry("ch1.pdf", 493_922), ctx)[0] == "new"


def test_small_files_are_not_adopted_by_size(ctx):
    """Below the floor, a shared byte count is plausible coincidence."""
    ctx["adopt_by_size"][100] = ["A/Materials/tiny.txt"]
    assert plan(entry("other.txt", 100), ctx)[0] == "new"


def test_size_floor_boundary_is_inclusive(ctx):
    ctx["adopt_by_size"][m.SIZE_ADOPT_FLOOR] = ["A/Materials/edge.pdf"]
    assert plan(entry("edge-renamed.pdf", m.SIZE_ADOPT_FLOOR), ctx)[0] == "adopt"


def test_adopted_file_also_suppresses_its_duplicate(ctx):
    """An adopted file must not then be downloaded as its own second copy."""
    ctx["adopt_index"][("outline.pdf", 34_000)] = "LLAW2012/Materials/Outline.pdf"
    assert plan(entry("outline.pdf", 34_000), ctx)[0] == "adopt"
    assert plan(entry("outline.pdf", 34_000), ctx)[0] == "dup"


# ------------------------------------------------------------------ placement

def test_destination_follows_course_and_section(ctx):
    action, dest = plan(entry("slides.pptx", 5_000), ctx, section="06 Lecture 1")
    assert action == "new"
    assert dest == ctx["inbox"] / "LLAW2012" / "06 Lecture 1" / "slides.pptx"


def test_existing_file_on_disk_is_not_overwritten(ctx):
    dest = ctx["inbox"] / "LLAW2012" / "06 Lecture 1" / "slides.pptx"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already here")

    action, chosen = plan(entry("slides.pptx", 5_000), ctx)
    assert action == "new"
    assert chosen.name == "slides (2).pptx"


def test_third_copy_increments_further(ctx):
    for expected in ("a.pdf", "a (2).pdf", "a (3).pdf"):
        action, dest = plan(entry("a.pdf", hash(expected) % 90_000 + 10_000), ctx)
        assert action == "new" and dest.name == expected
