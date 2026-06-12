"""Unit tests for the pure-logic helpers in generate_cards.py.

Network, image rendering, and file I/O are not covered here — these tests
target the deterministic text/HTML helpers. Run: pytest scripts/
"""

import generate_cards as gc


class TestStripTags:
    def test_removes_html_tags(self):
        assert gc.strip_tags("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self):
        assert gc.strip_tags("Tom &amp; Jerry") == "Tom & Jerry"

    def test_handles_none(self):
        assert gc.strip_tags(None) == ""


class TestCollapseWs:
    def test_collapses_runs_of_whitespace(self):
        assert gc.collapse_ws("a   b\n\tc") == "a b c"

    def test_strips_leading_and_trailing(self):
        assert gc.collapse_ws("  hi  ") == "hi"


class TestStripEmoji:
    def test_removes_emoji(self):
        # The 🥶 emoji (U+1F975) must be dropped; ASCII text retained.
        assert gc.strip_emoji("🥶 cold take") == "cold take"

    def test_keeps_plain_ascii(self):
        assert gc.strip_emoji("normal text 123") == "normal text 123"

    def test_keeps_latin_punctuation_dash(self):
        # An em dash (U+2014) is below the 0x2190 cutoff and should survive.
        assert "—" in gc.strip_emoji("AI—generated audio")


class TestMakeExcerpt:
    def test_returns_short_text_unchanged(self):
        assert gc.make_excerpt("short", 50) == "short"

    def test_truncates_on_word_boundary_with_ellipsis(self):
        result = gc.make_excerpt("one two three four five", max_len=12)
        # Cuts at the last space before the limit, then appends the ellipsis.
        assert result == "one two…"
        assert result.endswith("…")

    def test_falls_back_to_hard_cut_when_no_space(self):
        # A single long token has no space to break on.
        result = gc.make_excerpt("abcdefghijklmnop", max_len=8)
        assert result == "abcdefgh…"


class TestStripPhoton:
    def test_rewrites_photon_url_to_origin_and_drops_query(self):
        url = ("https://i0.wp.com/briangreenberg.net/wp-content/uploads/"
               "2026/05/AI-Micro-Transformations.jpg?resize=1024%2C572&ssl=1")
        assert gc.strip_photon(url) == (
            "https://briangreenberg.net/wp-content/uploads/"
            "2026/05/AI-Micro-Transformations.jpg")

    def test_rewrites_any_photon_shard_number(self):
        # Photon shards across i0/i1/i2; match any single digit.
        assert gc.strip_photon("https://i2.wp.com/example.net/a.png") == \
            "https://example.net/a.png"

    def test_drops_unescaped_entity_ampersand_query(self):
        # WP feeds embed &#038; (HTML entity for &) inside src attributes;
        # the query is dropped wholesale so the entity never reaches urllib.
        url = "https://i0.wp.com/example.net/img.jpg?resize=1%2C2&#038;ssl=1"
        assert gc.strip_photon(url) == "https://example.net/img.jpg"

    def test_leaves_origin_url_unchanged(self):
        url = "https://briangreenberg.net/wp-content/uploads/banner.jpeg"
        assert gc.strip_photon(url) == url

    def test_leaves_non_photon_cdn_unchanged(self):
        url = "https://media.infosec.exchange/accounts/avatars/avatar.png"
        assert gc.strip_photon(url) == url

    def test_does_not_match_photon_lookalike_hosts(self):
        # Host must be exactly iN.wp.com — a subdomain prefix or a longer
        # registrable domain must not be rewritten.
        for url in ("https://noti0.wp.com/example.net/a.png",
                    "https://i0.wp.com.evil.net/example.net/a.png"):
            assert gc.strip_photon(url) == url

    def test_leaves_empty_string_unchanged(self):
        # Blog posts with no image pass "" through to the placeholder path.
        assert gc.strip_photon("") == ""


class TestIsBareUrl:
    def test_true_for_lone_url(self):
        assert gc.is_bare_url("https://example.com/page?x=1") is True

    def test_false_when_caption_present(self):
        assert gc.is_bare_url("Check this https://example.com") is False

    def test_false_for_plain_text(self):
        assert gc.is_bare_url("just words") is False


class TestUpdateSection:
    def test_replaces_only_marked_region(self):
        readme = "before\n<!-- X:START -->\nold\n<!-- X:END -->\nafter"
        out = gc.update_section(readme, "X", "NEW")
        assert "before" in out and "after" in out
        assert "old" not in out
        assert "<!-- X:START -->\nNEW\n<!-- X:END -->" in out

    def test_is_idempotent(self):
        readme = "<!-- X:START -->\nA\n<!-- X:END -->"
        once = gc.update_section(readme, "X", "B")
        twice = gc.update_section(once, "X", "B")
        assert once == twice


class TestCardsToHtml:
    def test_emits_anchor_with_new_tab_and_alt(self):
        cards = [
            gc.Card(asset_path=None, rel_src="assets/c1.png",
                    url="https://example.com/post", alt='A "quoted" title'),
        ]
        html_out = gc.cards_to_html(cards)
        assert 'href="https://example.com/post"' in html_out
        assert 'target="_blank"' in html_out
        assert 'rel="noopener noreferrer"' in html_out
        assert f'width="{gc.DISPLAY_W}"' in html_out
        # Alt text must be HTML-escaped to survive quotes.
        assert "&quot;quoted&quot;" in html_out

    def test_centers_with_p_tag(self):
        cards = [gc.Card(asset_path=None, rel_src="a.png", url="u", alt="x")]
        assert gc.cards_to_html(cards).startswith('<p align="center">')
