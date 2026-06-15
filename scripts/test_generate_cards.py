"""Unit tests for the pure-logic helpers in generate_cards.py.

Network, image rendering, and file I/O are not covered here — these tests
target the deterministic text/HTML helpers. Run: pytest scripts/
"""

import xml.etree.ElementTree as ET

import generate_cards as gc

MEDIA = gc.NS_MEDIA  # "{http://search.yahoo.com/mrss/}"


def _item(*, media: str = "", body: str = "", link: str = "https://infosec.exchange/p/1") -> ET.Element:
    """Build a minimal RSS <item> element for hero-selection tests.

    ``media`` is raw ``<media:content .../>`` markup (zero or more elements).
    """
    xml = (
        '<item xmlns:media="http://search.yahoo.com/mrss/">'
        f"<link>{link}</link>"
        f"<description>{body}</description>"
        f"{media}"
        "</item>"
    )
    return ET.fromstring(xml)


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


class TestAssetVersion:
    def test_returns_8_hex_chars(self, tmp_path):
        f = tmp_path / "card.png"
        f.write_bytes(b"image-bytes")
        v = gc.asset_version(f)
        assert len(v) == 8
        assert all(c in "0123456789abcdef" for c in v)

    def test_same_content_same_version(self, tmp_path):
        # Deterministic: unchanged bytes must produce an unchanged URL, so
        # a no-op regeneration doesn't churn the README.
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(b"same")
        b.write_bytes(b"same")
        assert gc.asset_version(a) == gc.asset_version(b)

    def test_changed_content_changes_version(self, tmp_path):
        # The whole point: new bytes → new URL → every cache layer misses.
        f = tmp_path / "card.png"
        f.write_bytes(b"old card")
        old = gc.asset_version(f)
        f.write_bytes(b"new card")
        assert gc.asset_version(f) != old


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


class TestMediaKind:
    def test_classifies_by_medium_attribute(self):
        m = ET.fromstring('<content xmlns="x" medium="video"/>')
        assert gc.media_kind(m) == "video"

    def test_falls_back_to_mime_type_prefix(self):
        # Some feeds omit `medium` but always carry a MIME `type`.
        m = ET.fromstring('<content xmlns="x" type="image/jpeg"/>')
        assert gc.media_kind(m) == "image"

    def test_medium_takes_precedence_and_is_case_insensitive(self):
        m = ET.fromstring('<content xmlns="x" medium="VIDEO" type="image/png"/>')
        assert gc.media_kind(m) == "video"

    def test_unknown_attachment_returns_empty_string(self):
        m = ET.fromstring('<content xmlns="x" type="application/pdf"/>')
        assert gc.media_kind(m) == ""

    def test_no_attributes_returns_empty_string(self):
        m = ET.fromstring('<content xmlns="x"/>')
        assert gc.media_kind(m) == ""


class TestFirstArticleLink:
    def test_returns_external_article_link(self):
        body = ('<p>Great read <a href="https://www.theverge.com/x" '
                'target="_blank" rel="nofollow noopener">theverge.com</a></p>')
        assert gc.first_article_link(body) == "https://www.theverge.com/x"

    def test_skips_hashtag_anchors(self):
        # Mastodon hashtags carry class="mention hashtag".
        body = ('<a href="https://infosec.exchange/tags/InfoSec" '
                'class="mention hashtag" rel="tag">#InfoSec</a>')
        assert gc.first_article_link(body) is None

    def test_skips_user_mention_anchors(self):
        # @-mentions carry class="u-url mention" — even cross-instance ones.
        body = ('<a href="https://mastodon.social/@someone" '
                'class="u-url mention">@someone</a>')
        assert gc.first_article_link(body) is None

    def test_picks_article_over_trailing_hashtags(self):
        body = (
            '<p>News <a href="https://gizmodo.com/story" target="_blank" '
            'rel="nofollow noopener">gizmodo.com</a></p>'
            '<a href="https://infosec.exchange/tags/AI" class="mention hashtag" rel="tag">#AI</a>'
        )
        assert gc.first_article_link(body) == "https://gizmodo.com/story"

    def test_unescapes_entities_in_href(self):
        body = '<a href="https://ex.com/a?x=1&amp;y=2" target="_blank">link</a>'
        assert gc.first_article_link(body) == "https://ex.com/a?x=1&y=2"

    def test_returns_none_for_plain_text(self):
        assert gc.first_article_link("<p>just words, no links</p>") is None


class TestMastoHeroUrl:
    def test_prefers_native_image_attachment(self, monkeypatch):
        # og_image must NOT be consulted when a real image attachment exists.
        monkeypatch.setattr(gc, "og_image", lambda url: "should-not-be-used")
        item = _item(media='<media:content url="https://cdn/x.jpg" medium="image" type="image/jpeg"/>')
        assert gc.masto_hero_url(item, "https://post", "") == "https://cdn/x.jpg"

    def test_video_attachment_uses_post_og_image_poster_frame(self, monkeypatch):
        # The .mp4 URL must never be returned; the post's og:image poster is.
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://cdn/poster.png" if url == "https://post" else None)
        monkeypatch.setattr(gc, "usable_image", lambda url: True)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video" type="video/mp4"/>')
        assert gc.masto_hero_url(item, "https://post", "") == "https://cdn/poster.png"

    def test_blank_video_poster_falls_through_to_avatar(self, monkeypatch):
        # Observed real case: Mastodon returns a flat #f2f2f2 poster for a
        # video with no thumbnail. usable_image rejects it; with no article
        # link, the card falls back to the avatar rather than a blank box.
        monkeypatch.setattr(gc, "og_image", lambda url: "https://cdn/blank.png")
        monkeypatch.setattr(gc, "usable_image", lambda url: False)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>')
        assert gc.masto_hero_url(item, "https://post", "") == gc.MASTO_AVATAR

    def test_blank_video_poster_falls_through_to_article_link(self, monkeypatch):
        # Blank poster but the post also links an article → use the article.
        monkeypatch.setattr(gc, "usable_image", lambda url: False)
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://verge/hero.png" if "theverge" in url else "https://cdn/blank.png")
        body = '<p>watch <a href="https://www.theverge.com/x" target="_blank">vg</a></p>'
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>', body=body)
        assert gc.masto_hero_url(item, "https://post", body) == "https://verge/hero.png"

    def test_link_post_uses_article_og_image(self, monkeypatch):
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://verge/hero.png" if "theverge" in url else None)
        body = '<p>read <a href="https://www.theverge.com/x" target="_blank">vg</a></p>'
        assert gc.masto_hero_url(_item(body=body), "https://post", body) == "https://verge/hero.png"

    def test_image_attachment_wins_over_video_when_both_present(self, monkeypatch):
        monkeypatch.setattr(gc, "og_image", lambda url: "poster")
        item = _item(
            media='<media:content url="https://cdn/v.mp4" medium="video"/>'
                  '<media:content url="https://cdn/x.jpg" medium="image"/>'
        )
        assert gc.masto_hero_url(item, "https://post", "") == "https://cdn/x.jpg"

    def test_falls_back_to_avatar_for_pure_text_post(self, monkeypatch):
        # No media, no link, and og_image yields nothing → account avatar.
        monkeypatch.setattr(gc, "og_image", lambda url: None)
        item = _item(body="<p>just a thought, no link</p>")
        assert gc.masto_hero_url(item, "https://post", "<p>just a thought</p>") == gc.MASTO_AVATAR

    def test_falls_back_to_avatar_when_video_poster_scrape_fails(self, monkeypatch):
        # Video present but og:image scrape returns None and there's no link.
        monkeypatch.setattr(gc, "og_image", lambda url: None)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>')
        assert gc.masto_hero_url(item, "https://post", "") == gc.MASTO_AVATAR


class TestUsableImage:
    @staticmethod
    def _png_bytes(img):
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_rejects_flat_single_color_poster(self, monkeypatch):
        from PIL import Image
        flat = Image.new("RGB", (64, 64), (242, 242, 242))  # the observed blank poster
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: self._png_bytes(flat))
        assert gc.usable_image("https://cdn/blank.png") is False

    def test_accepts_image_with_real_content(self, monkeypatch):
        from PIL import Image
        grad = Image.linear_gradient("L").convert("RGB")  # 0→255 spread
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: self._png_bytes(grad))
        assert gc.usable_image("https://cdn/photo.png") is True

    def test_returns_false_on_fetch_error(self, monkeypatch):
        def boom(url, **kw):
            raise OSError("network down")
        monkeypatch.setattr(gc, "fetch_url", boom)
        assert gc.usable_image("https://cdn/x.png") is False


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
