"""Tests for the HTML handling and page-text extraction.

The case that matters most: MATH2012's "Important Messages" section is 4,199
characters of HTML in which *every* character sits inside an HTML comment —
last year's Zoom links, a different tutor, different tutorial groups, "after the
Chinese New Year holiday". The lecturer hid it rather than deleting it. Naive
tag-stripping resurrects all of it as this year's arrangements, and there is
nothing in the output to suggest anything is wrong.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import moodle_sync as m


# ------------------------------------------------------------ hidden content

def test_commented_out_content_is_not_visible():
    raw = "<p>Current notice</p><!-- <p>Zoom link: https://hku.zoom.us/j/908</p> -->"
    assert "Current notice" in m.html_to_text(raw)
    assert "zoom" not in m.html_to_text(raw).lower()


def test_comment_containing_tags_does_not_leak():
    """The original bug. `<[^>]+>` shreds such a comment and leaks its text."""
    raw = "<!-- <p>Tutor: Dr Cheung</p><p>Group 1: Tue 11:30</p> -->Real content"
    out = m.html_to_text(raw)
    assert "Dr Cheung" not in out
    assert "Group 1" not in out
    assert "-->" not in out, "stray comment terminator leaked into the text"
    assert out == "Real content"


def test_unclosed_comment_hides_to_the_end():
    """MATH2012's summary has 4 openers and 3 closers — real markup is malformed."""
    raw = "Visible<!-- hidden and never closed <p>more hidden</p>"
    assert m.html_to_text(raw) == "Visible"


def test_wholly_commented_blob_yields_nothing():
    assert m.html_to_text("<!-- <div><p>all of it</p></div> -->") == ""


def test_strip_html_also_respects_comments():
    """strip_html feeds manifest entries, so it had the same bug."""
    assert "hidden" not in m.strip_html("shown <!-- hidden -->")


# ------------------------------------------------------------------ structure

def test_line_structure_survives():
    raw = "<p>First lecture Sep 3</p><p>12:00 at KB223</p>"
    assert m.html_to_text(raw).splitlines() == ["First lecture Sep 3", "12:00 at KB223"]


def test_list_items_become_bullets():
    assert "- Tue 9:00" in m.html_to_text("<ul><li>Tue 9:00</li></ul>")


def test_entities_and_nbsp_are_decoded():
    assert m.html_to_text("A&nbsp;&amp;&nbsp;B") == "A & B"


# ----------------------------------------------------------- embedded files

def test_embedded_link_is_rewritten_to_the_webservice_path():
    """The browser form ignores the token and returns an HTML error page."""
    raw = ('<a href="https://moodle.hku.hk/pluginfile.php/6714300/mod_label/intro/'
           'Course%20information.pdf">Course Information</a>')
    (name, url), = m.embedded_files(raw)
    assert name == "Course information.pdf"
    assert url.startswith("https://moodle.hku.hk/webservice/pluginfile.php/")


def test_already_webservice_urls_are_left_alone():
    raw = '<a href="https://moodle.hku.hk/webservice/pluginfile.php/1/x/a.pdf">a</a>'
    (_, url), = m.embedded_files(raw)
    assert url.count("/webservice/") == 1


def test_embedded_links_are_deduplicated_within_a_blob():
    """MATH2012 links the same Course Information PDF from several labels."""
    one = '<a href="https://moodle.hku.hk/pluginfile.php/1/mod_label/intro/a.pdf">x</a>'
    assert len(m.embedded_files(one + one)) == 1


def test_hidden_embedded_links_are_not_fetched():
    raw = '<!-- <a href="https://moodle.hku.hk/pluginfile.php/1/mod_label/intro/old.pdf">x</a> -->'
    assert m.embedded_files(raw) == []


def test_query_and_fragment_are_dropped():
    raw = '<a href="https://moodle.hku.hk/pluginfile.php/1/m/i/a.pdf?forcedownload=1#p2">a</a>'
    (name, url), = m.embedded_files(raw)
    assert name == "a.pdf" and "?" not in url and "#" not in url


# -------------------------------------------------------------- page blocks

def test_page_blocks_collects_summaries_and_labels():
    sections = [{
        "name": "Tutorials",
        "summary": "<p>Section note</p>",
        "modules": [
            {"modname": "label", "name": "L", "description": "<p>Deadline is Sep 11</p>"},
            {"modname": "choice", "name": "Tutorial Registration",
             "description": "<p>First come first served</p>"},
            {"modname": "forum", "name": "Announcements", "description": ""},
        ],
    }]
    blocks = m.page_blocks(sections)
    assert ("Tutorials", "section text", "Section note") in blocks
    assert ("Tutorials", "label", "Deadline is Sep 11") in blocks
    assert ("Tutorials", "choice: Tutorial Registration", "First come first served") in blocks
    assert len(blocks) == 3, "an empty description must not become a block"


def test_page_blocks_excludes_hidden_text():
    sections = [{"name": "S", "summary": "<!-- <p>last year</p> -->", "modules": []}]
    assert m.page_blocks(sections) == []
