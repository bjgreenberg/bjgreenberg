"""Unit tests for the pure-logic helpers in generate_cards.py.

Network I/O is not covered here — these tests target the deterministic
text/HTML helpers; image rendering gets geometry smoke tests (canvas size,
files written), not pixel assertions. Run: pytest scripts/
"""

import io
import shutil
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import NoReturn

import pytest
from PIL import Image

import generate_cards as gc

MEDIA = gc.NS_MEDIA  # "{http://search.yahoo.com/mrss/}"


def _days(start_iso: str, counts: list[int]) -> list[gc.ContribDay]:
    """Build a contiguous day-by-day calendar starting at start_iso."""
    from datetime import datetime, timedelta
    d0 = datetime.strptime(start_iso, "%Y-%m-%d").date()
    return [gc.ContribDay(date=(d0 + timedelta(days=i)).isoformat(), count=c)
            for i, c in enumerate(counts)]


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
    def test_removes_html_tags(self) -> None:
        assert gc.strip_tags("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self) -> None:
        assert gc.strip_tags("Tom &amp; Jerry") == "Tom & Jerry"

    def test_handles_none(self) -> None:
        assert gc.strip_tags(None) == ""


class TestCollapseWs:
    def test_collapses_runs_of_whitespace(self) -> None:
        assert gc.collapse_ws("a   b\n\tc") == "a b c"

    def test_strips_leading_and_trailing(self) -> None:
        assert gc.collapse_ws("  hi  ") == "hi"


class TestStripEmoji:
    def test_removes_emoji(self) -> None:
        # The 🥶 emoji (U+1F975) must be dropped; ASCII text retained.
        assert gc.strip_emoji("🥶 cold take") == "cold take"

    def test_keeps_plain_ascii(self) -> None:
        assert gc.strip_emoji("normal text 123") == "normal text 123"

    def test_keeps_latin_punctuation_dash(self) -> None:
        # An em dash (U+2014) is below the 0x2190 cutoff and should survive.
        assert "—" in gc.strip_emoji("AI—generated audio")


class TestMakeExcerpt:
    def test_returns_short_text_unchanged(self) -> None:
        assert gc.make_excerpt("short", 50) == "short"

    def test_truncates_on_word_boundary_with_ellipsis(self) -> None:
        result = gc.make_excerpt("one two three four five", max_len=12)
        # Cuts at the last space before the limit, then appends the ellipsis.
        assert result == "one two…"
        assert result.endswith("…")

    def test_falls_back_to_hard_cut_when_no_space(self) -> None:
        # A single long token has no space to break on.
        result = gc.make_excerpt("abcdefghijklmnop", max_len=8)
        assert result == "abcdefgh…"


class TestStripPhoton:
    def test_rewrites_photon_url_to_origin_and_drops_query(self) -> None:
        url = ("https://i0.wp.com/briangreenberg.net/wp-content/uploads/"
               "2026/05/AI-Micro-Transformations.jpg?resize=1024%2C572&ssl=1")
        assert gc.strip_photon(url) == (
            "https://briangreenberg.net/wp-content/uploads/"
            "2026/05/AI-Micro-Transformations.jpg")

    def test_rewrites_any_photon_shard_number(self) -> None:
        # Photon shards across i0/i1/i2; match any single digit.
        assert gc.strip_photon("https://i2.wp.com/example.net/a.png") == \
            "https://example.net/a.png"

    def test_drops_unescaped_entity_ampersand_query(self) -> None:
        # WP feeds embed &#038; (HTML entity for &) inside src attributes;
        # the query is dropped wholesale so the entity never reaches urllib.
        url = "https://i0.wp.com/example.net/img.jpg?resize=1%2C2&#038;ssl=1"
        assert gc.strip_photon(url) == "https://example.net/img.jpg"

    def test_leaves_origin_url_unchanged(self) -> None:
        url = "https://briangreenberg.net/wp-content/uploads/banner.jpeg"
        assert gc.strip_photon(url) == url

    def test_leaves_non_photon_cdn_unchanged(self) -> None:
        url = "https://media.infosec.exchange/accounts/avatars/avatar.png"
        assert gc.strip_photon(url) == url

    def test_does_not_match_photon_lookalike_hosts(self) -> None:
        # Host must be exactly iN.wp.com — a subdomain prefix or a longer
        # registrable domain must not be rewritten.
        for url in ("https://noti0.wp.com/example.net/a.png",
                    "https://i0.wp.com.evil.net/example.net/a.png"):
            assert gc.strip_photon(url) == url

    def test_leaves_empty_string_unchanged(self) -> None:
        # Blog posts with no image pass "" through to the placeholder path.
        assert gc.strip_photon("") == ""


class TestAssetVersion:
    def test_returns_8_hex_chars(self, tmp_path: Path) -> None:
        f = tmp_path / "card.png"
        f.write_bytes(b"image-bytes")
        v = gc.asset_version(f)
        assert len(v) == 8
        assert all(c in "0123456789abcdef" for c in v)

    def test_same_content_same_version(self, tmp_path: Path) -> None:
        # Deterministic: unchanged bytes must produce an unchanged URL, so
        # a no-op regeneration doesn't churn the README.
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(b"same")
        b.write_bytes(b"same")
        assert gc.asset_version(a) == gc.asset_version(b)

    def test_changed_content_changes_version(self, tmp_path: Path) -> None:
        # The whole point: new bytes → new URL → every cache layer misses.
        f = tmp_path / "card.png"
        f.write_bytes(b"old card")
        old = gc.asset_version(f)
        f.write_bytes(b"new card")
        assert gc.asset_version(f) != old


class TestIsBareUrl:
    def test_true_for_lone_url(self) -> None:
        assert gc.is_bare_url("https://example.com/page?x=1") is True

    def test_false_when_caption_present(self) -> None:
        assert gc.is_bare_url("Check this https://example.com") is False

    def test_false_for_plain_text(self) -> None:
        assert gc.is_bare_url("just words") is False


class TestUpdateSection:
    def test_replaces_only_marked_region(self) -> None:
        readme = "before\n<!-- X:START -->\nold\n<!-- X:END -->\nafter"
        out = gc.update_section(readme, "X", "NEW")
        assert "before" in out and "after" in out
        assert "old" not in out
        assert "<!-- X:START -->\nNEW\n<!-- X:END -->" in out

    def test_is_idempotent(self) -> None:
        readme = "<!-- X:START -->\nA\n<!-- X:END -->"
        once = gc.update_section(readme, "X", "B")
        twice = gc.update_section(once, "X", "B")
        assert once == twice


class TestReadmeStamp:
    NOW = datetime(2026, 9, 18, 21, 30, tzinfo=timezone.utc)  # 4:30 PM CDT

    @staticmethod
    def _readme(body: str, stamp: str = "_Last updated: 2026-01-01 09:00 AM CST_") -> str:
        return (f"<!-- X:START -->\n{body}\n<!-- X:END -->\n"
                f"<!-- README-STAMP:START -->\n{stamp}\n<!-- README-STAMP:END -->")

    def test_stamp_uses_fleet_format_in_chicago_time(self) -> None:
        assert gc.readme_stamp(self.NOW) == "_Last updated: 2026-09-18 04:30 PM CDT_"

    def test_stamp_matches_the_docs_stamp_gate_pattern(self) -> None:
        # developer-handbook check-doc-stamps.py STAMP_RE, verbatim.
        import re
        assert re.search(r"^\s*[_*]{0,2}\s*Last updated:\s*\S", gc.readme_stamp(self.NOW), re.M)

    def test_moves_when_content_changed(self) -> None:
        original = self._readme("old")
        updated = gc.update_section(original, "X", "new")
        out = gc.restamp(original, updated, self.NOW)
        assert "_Last updated: 2026-09-18 04:30 PM CDT_" in out
        assert "new" in out

    def test_stays_put_when_nothing_else_changed(self) -> None:
        original = self._readme("same")
        updated = gc.update_section(original, "X", "same")
        assert gc.restamp(original, updated, self.NOW) == original

    def test_missing_markers_leave_readme_untouched(self) -> None:
        assert gc.restamp("a", "b", self.NOW) == "b"

    def test_real_readme_carries_the_markers(self) -> None:
        # Without them the generator silently writes no stamp — fail loudly here.
        text = gc.README_PATH.read_text()
        assert "<!-- README-STAMP:START -->" in text
        assert "<!-- README-STAMP:END -->" in text


class TestMediaKind:
    def test_classifies_by_medium_attribute(self) -> None:
        m = ET.fromstring('<content xmlns="x" medium="video"/>')
        assert gc.media_kind(m) == "video"

    def test_falls_back_to_mime_type_prefix(self) -> None:
        # Some feeds omit `medium` but always carry a MIME `type`.
        m = ET.fromstring('<content xmlns="x" type="image/jpeg"/>')
        assert gc.media_kind(m) == "image"

    def test_medium_takes_precedence_and_is_case_insensitive(self) -> None:
        m = ET.fromstring('<content xmlns="x" medium="VIDEO" type="image/png"/>')
        assert gc.media_kind(m) == "video"

    def test_unknown_attachment_returns_empty_string(self) -> None:
        m = ET.fromstring('<content xmlns="x" type="application/pdf"/>')
        assert gc.media_kind(m) == ""

    def test_no_attributes_returns_empty_string(self) -> None:
        m = ET.fromstring('<content xmlns="x"/>')
        assert gc.media_kind(m) == ""


class TestFirstArticleLink:
    def test_returns_external_article_link(self) -> None:
        body = ('<p>Great read <a href="https://www.theverge.com/x" '
                'target="_blank" rel="nofollow noopener">theverge.com</a></p>')
        assert gc.first_article_link(body) == "https://www.theverge.com/x"

    def test_skips_hashtag_anchors(self) -> None:
        # Mastodon hashtags carry class="mention hashtag".
        body = ('<a href="https://infosec.exchange/tags/InfoSec" '
                'class="mention hashtag" rel="tag">#InfoSec</a>')
        assert gc.first_article_link(body) is None

    def test_skips_user_mention_anchors(self) -> None:
        # @-mentions carry class="u-url mention" — even cross-instance ones.
        body = ('<a href="https://mastodon.social/@someone" '
                'class="u-url mention">@someone</a>')
        assert gc.first_article_link(body) is None

    def test_picks_article_over_trailing_hashtags(self) -> None:
        body = (
            '<p>News <a href="https://gizmodo.com/story" target="_blank" '
            'rel="nofollow noopener">gizmodo.com</a></p>'
            '<a href="https://infosec.exchange/tags/AI" class="mention hashtag" rel="tag">#AI</a>'
        )
        assert gc.first_article_link(body) == "https://gizmodo.com/story"

    def test_unescapes_entities_in_href(self) -> None:
        body = '<a href="https://ex.com/a?x=1&amp;y=2" target="_blank">link</a>'
        assert gc.first_article_link(body) == "https://ex.com/a?x=1&y=2"

    def test_returns_none_for_plain_text(self) -> None:
        assert gc.first_article_link("<p>just words, no links</p>") is None


class TestMastoHero:
    def test_prefers_native_image_attachment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # og_image must NOT be consulted when a real image attachment exists.
        monkeypatch.setattr(gc, "og_image", lambda url: "should-not-be-used")
        item = _item(media='<media:content url="https://cdn/x.jpg" medium="image" type="image/jpeg"/>')
        assert gc.masto_hero(item, "https://post", "") == "https://cdn/x.jpg"

    def test_video_attachment_uses_ffmpeg_frame_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An extracted frame (bytes) is the preferred video hero; the .mp4 URL
        # and og:image poster must never be used when a frame is available.
        monkeypatch.setattr(gc, "extract_video_frame", lambda url: b"\x89PNG-frame")
        monkeypatch.setattr(gc, "og_image", lambda url: "should-not-be-used")
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video" type="video/mp4"/>')
        assert gc.masto_hero(item, "https://post", "") == b"\x89PNG-frame"

    def test_video_falls_back_to_poster_when_ffmpeg_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ffmpeg unavailable / all frames blank → use the instance poster.
        monkeypatch.setattr(gc, "extract_video_frame", lambda url: None)
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://cdn/poster.png" if url == "https://post" else None)
        monkeypatch.setattr(gc, "usable_image", lambda url: True)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video" type="video/mp4"/>')
        assert gc.masto_hero(item, "https://post", "") == "https://cdn/poster.png"

    def test_blank_video_poster_falls_through_to_avatar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No ffmpeg frame, blank poster, no link → avatar, not a blank box.
        monkeypatch.setattr(gc, "extract_video_frame", lambda url: None)
        monkeypatch.setattr(gc, "og_image", lambda url: "https://cdn/blank.png")
        monkeypatch.setattr(gc, "usable_image", lambda url: False)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>')
        assert gc.masto_hero(item, "https://post", "") == gc.MASTO_AVATAR

    def test_blank_video_poster_falls_through_to_article_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No frame, blank poster, no instance card, but the post links an article.
        monkeypatch.setattr(gc, "extract_video_frame", lambda url: None)
        monkeypatch.setattr(gc, "usable_image", lambda url: False)
        monkeypatch.setattr(gc, "masto_card_image", lambda url: None)
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://verge/hero.png" if "theverge" in url else "https://cdn/blank.png")
        body = '<p>watch <a href="https://www.theverge.com/x" target="_blank">vg</a></p>'
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>', body=body)
        assert gc.masto_hero(item, "https://post", body) == "https://verge/hero.png"

    def test_link_post_prefers_mastodon_preview_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The instance's cached preview (on its CDN) wins over scraping the
        # news site, which is unreliable from CI.
        monkeypatch.setattr(gc, "masto_card_image", lambda url: "https://cdn.instance/preview.png")
        monkeypatch.setattr(gc, "og_image", lambda url: "should-not-be-used")
        body = '<p>read <a href="https://www.theverge.com/x" target="_blank">vg</a></p>'
        assert gc.masto_hero(_item(body=body), "https://post/1", body) == "https://cdn.instance/preview.png"

    def test_link_post_falls_back_to_article_scrape_without_preview_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No instance card → scrape the linked article's og:image.
        monkeypatch.setattr(gc, "masto_card_image", lambda url: None)
        monkeypatch.setattr(gc, "og_image",
                            lambda url: "https://verge/hero.png" if "theverge" in url else None)
        body = '<p>read <a href="https://www.theverge.com/x" target="_blank">vg</a></p>'
        assert gc.masto_hero(_item(body=body), "https://post/1", body) == "https://verge/hero.png"

    def test_image_attachment_wins_over_video_when_both_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Image returns before the video branch, so ffmpeg is never invoked.
        monkeypatch.setattr(gc, "extract_video_frame",
                            lambda url: (_ for _ in ()).throw(AssertionError("ffmpeg must not run")))
        item = _item(
            media='<media:content url="https://cdn/v.mp4" medium="video"/>'
                  '<media:content url="https://cdn/x.jpg" medium="image"/>'
        )
        assert gc.masto_hero(item, "https://post", "") == "https://cdn/x.jpg"

    def test_falls_back_to_avatar_for_pure_text_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No media, no instance card, no link, no og:image → account avatar.
        monkeypatch.setattr(gc, "og_image", lambda url: None)
        monkeypatch.setattr(gc, "masto_card_image", lambda url: None)
        item = _item(body="<p>just a thought, no link</p>")
        assert gc.masto_hero(item, "https://post", "<p>just a thought</p>") == gc.MASTO_AVATAR

    def test_falls_back_to_avatar_when_video_has_no_frame_or_poster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Video present but ffmpeg + poster scrape both yield nothing, no link.
        monkeypatch.setattr(gc, "extract_video_frame", lambda url: None)
        monkeypatch.setattr(gc, "og_image", lambda url: None)
        monkeypatch.setattr(gc, "masto_card_image", lambda url: None)
        item = _item(media='<media:content url="https://cdn/v.mp4" medium="video"/>')
        assert gc.masto_hero(item, "https://post", "") == gc.MASTO_AVATAR


class TestMastoCardImage:
    def test_returns_card_image_from_status_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b'{"card": {"type": "link", "image": "https://cdn.instance/p.png"}}'
        captured: dict[str, str] = {}

        def fake_fetch(url: str, **kw: object) -> bytes:
            captured["url"] = url
            return payload
        monkeypatch.setattr(gc, "fetch_url", fake_fetch)
        out = gc.masto_card_image("https://infosec.exchange/@brian/116749028717068067")
        assert out == "https://cdn.instance/p.png"
        # The status id is the trailing numeric path segment.
        assert captured["url"] == "https://infosec.exchange/api/v1/statuses/116749028717068067"

    def test_returns_none_when_status_has_no_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Video/media posts carry card=null.
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: b'{"card": null}')
        assert gc.masto_card_image("https://infosec.exchange/@brian/123") is None

    def test_returns_none_when_card_has_no_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: b'{"card": {"type": "link"}}')
        assert gc.masto_card_image("https://infosec.exchange/@brian/123") is None

    def test_returns_none_without_network_when_no_status_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A permalink with no trailing numeric id must not trigger a fetch.
        def boom(url: str, **kw: object) -> NoReturn:
            raise AssertionError("fetch_url must not be called")
        monkeypatch.setattr(gc, "fetch_url", boom)
        assert gc.masto_card_image("https://infosec.exchange/@brian_greenberg") is None

    def test_returns_none_on_fetch_or_json_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(url: str, **kw: object) -> NoReturn:
            raise OSError("api down")
        monkeypatch.setattr(gc, "fetch_url", boom)
        assert gc.masto_card_image("https://infosec.exchange/@brian/123") is None


class TestUsableImage:
    @staticmethod
    def _png_bytes(img: Image.Image) -> bytes:
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_rejects_flat_single_color_poster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PIL import Image
        flat = Image.new("RGB", (64, 64), (242, 242, 242))  # the observed blank poster
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: self._png_bytes(flat))
        assert gc.usable_image("https://cdn/blank.png") is False

    def test_accepts_image_with_real_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PIL import Image
        grad = Image.linear_gradient("L").convert("RGB")  # 0→255 spread
        monkeypatch.setattr(gc, "fetch_url", lambda url, **kw: self._png_bytes(grad))
        assert gc.usable_image("https://cdn/photo.png") is True

    def test_returns_false_on_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(url: str, **kw: object) -> NoReturn:
            raise OSError("network down")
        monkeypatch.setattr(gc, "fetch_url", boom)
        assert gc.usable_image("https://cdn/x.png") is False


class TestComputeActivityStats:
    TODAY = date(2026, 6, 15)

    def test_empty_calendar_is_all_zero(self) -> None:
        s = gc.compute_activity_stats([], self.TODAY)
        assert s["total"] == 0 and s["current_streak"] == 0 and s["longest_streak"] == 0
        assert s["since_year"] == 0

    def test_total_and_since_year(self) -> None:
        s = gc.compute_activity_stats(_days("2024-01-01", [1, 2, 0, 3]), self.TODAY)
        assert s["total"] == 6
        assert s["since_year"] == 2024

    def test_current_streak_ending_today(self) -> None:
        # 4 contiguous days ending exactly on TODAY.
        s = gc.compute_activity_stats(_days("2026-06-12", [5, 5, 5, 5]), self.TODAY)
        assert s["current_streak"] == 4
        assert s["current_start"] == "2026-06-12"
        assert s["current_end"] == "2026-06-15"

    def test_today_with_no_contributions_yet_does_not_break_streak(self) -> None:
        # Days through yesterday have activity; today (last day) is 0 → grace.
        s = gc.compute_activity_stats(_days("2026-06-12", [5, 5, 5, 0]), self.TODAY)
        assert s["current_streak"] == 3
        assert s["current_end"] == "2026-06-14"  # yesterday

    def test_streak_broken_when_latest_activity_older_than_yesterday(self) -> None:
        # Last activity was 2026-06-12, then two zero days (13th, 14th) and today.
        s = gc.compute_activity_stats(_days("2026-06-10", [5, 5, 5, 0, 0, 0]), self.TODAY)
        assert s["current_streak"] == 0
        assert s["current_start"] == ""

    def test_longest_streak_with_a_gap(self) -> None:
        # run of 3, gap, run of 5 → longest is 5.
        s = gc.compute_activity_stats(
            _days("2026-01-01", [1, 1, 1, 0, 2, 2, 2, 2, 2]), self.TODAY)
        assert s["longest_streak"] == 5
        assert s["longest_start"] == "2026-01-05"
        assert s["longest_end"] == "2026-01-09"

    def test_future_days_are_ignored(self) -> None:
        # The current-year calendar includes future zero days through Dec 31;
        # they must not be read as a broken trailing streak.
        cal = _days("2026-06-13", [4, 4, 4, 0, 0, 0, 0])  # 13,14,15 active; 16+ future zeros
        s = gc.compute_activity_stats(cal, self.TODAY)
        assert s["current_streak"] == 3
        assert s["current_end"] == "2026-06-15"
        assert s["total"] == 12  # future zeros excluded (they were 0 anyway)


class TestFeaturedMetaLine:
    @staticmethod
    def _meta(**over: object) -> gc.RepoMeta:
        base = gc.RepoMeta(description="A skill.", stars=98, forks=13,
                           license="Apache-2.0", release="v1.16.2",
                           language="Python")
        base.update(over)  # type: ignore[typeddict-item]
        return base

    def test_full_meta_joins_with_middots(self) -> None:
        assert gc.featured_meta_line(self._meta()) == \
            "Python · Apache-2.0 · v1.16.2 · 98 stars · 13 forks"

    def test_absent_fields_are_skipped_but_zero_counts_shown(self) -> None:
        line = gc.featured_meta_line(
            self._meta(license="", release="", language="", stars=0))
        assert line == "0 stars · 13 forks"

    def test_counts_use_thousands_separators(self) -> None:
        assert "1,234 stars" in gc.featured_meta_line(self._meta(stars=1234))


class TestRenderFeaturedCard:
    def test_smoke_renders_card_at_expected_size(self) -> None:
        meta = gc.RepoMeta(description="Words " * 60, stars=1, forks=0,
                           license="Apache-2.0", release="v1.0.0",
                           language="Python")
        img = gc.render_featured_card(meta, "bjgreenberg", "senior-engineering-partner")
        assert img.size == (gc.ACTIVITY_RENDER_W, gc.FEATURED_RENDER_H)


class TestExtractVideoFrame:
    def test_returns_none_when_ffmpeg_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        # Must not even attempt a download when ffmpeg isn't installed.
        monkeypatch.setattr(gc, "fetch_url",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
        assert gc.extract_video_frame("https://cdn/v.mp4") is None

    def test_returns_none_when_download_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
        def boom(*a: object, **k: object) -> NoReturn:
            raise OSError("network down")
        monkeypatch.setattr(gc, "fetch_url", boom)
        assert gc.extract_video_frame("https://cdn/v.mp4") is None


class TestActivityStamp:
    def test_summer_is_cdt_12_hour(self) -> None:
        # 2026-07-01 18:30 UTC → 13:30 CDT (UTC-5).
        dt = datetime(2026, 7, 1, 18, 30, tzinfo=timezone.utc)
        assert gc._activity_stamp(dt) == "Updated Jul 1, 2026 · 1:30 PM CDT"

    def test_winter_is_cst_and_noon_hour_not_stripped(self) -> None:
        # 2026-01-15 18:30 UTC → 12:30 CST (UTC-6); "12" must stay "12".
        dt = datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc)
        assert gc._activity_stamp(dt) == "Updated Jan 15, 2026 · 12:30 PM CST"


class TestImageHasContent:
    @staticmethod
    def _png(img: Image.Image) -> bytes:
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_flat_image_has_no_content(self) -> None:
        from PIL import Image
        assert gc._image_has_content(self._png(Image.new("RGB", (32, 32), (242, 242, 242)))) is False

    def test_gradient_has_content(self) -> None:
        from PIL import Image
        assert gc._image_has_content(self._png(Image.linear_gradient("L").convert("RGB"))) is True

    def test_undecodable_bytes_have_no_content(self) -> None:
        assert gc._image_has_content(b"not an image") is False


class TestFetchPhotoBytes:
    def test_accepts_pre_decoded_bytes_without_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io
        from PIL import Image
        monkeypatch.setattr(gc, "fetch_url",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, format="PNG")
        out = gc.fetch_photo(buf.getvalue(), 100, 50)
        assert out.size == (100, 50)

    def test_bad_bytes_yield_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = gc.fetch_photo(b"garbage", 60, 40)
        assert out.size == (60, 40)  # placeholder, no exception


class TestActivityFormatters:
    def test_fmt_date_drops_leading_zero_on_day(self) -> None:
        assert gc._fmt_date("2010-09-23") == "Sep 23, 2010"
        assert gc._fmt_date("2026-06-05") == "Jun 5, 2026"

    def test_fmt_range_current_ends_in_present(self) -> None:
        assert gc._fmt_range("2026-05-26", "2026-06-15", current=True) == "May 26, 2026 – Present"

    def test_fmt_range_past_shows_both_dates(self) -> None:
        assert gc._fmt_range("2026-03-10", "2026-04-21", current=False) == \
            "Mar 10, 2026 – Apr 21, 2026"

    def test_fmt_range_empty_is_dash(self) -> None:
        assert gc._fmt_range("", "", current=True) == "—"


class TestGithubToken:
    def test_prefers_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "fine-grained")
        monkeypatch.setenv("GITHUB_TOKEN", "actions")
        assert gc.github_token() == "fine-grained"

    def test_falls_back_to_github_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "actions")
        assert gc.github_token() == "actions"

    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert gc.github_token() is None


class TestBuildGithubCards:
    # Token gating lives in main() (one shared gate for the GitHub cards);
    # the builder just renders from pre-fetched days.

    def test_activity_card_renders_from_prefetched_days(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(gc, "ASSETS_DIR", tmp_path)
        now = datetime(2026, 6, 15, 16, 45, tzinfo=timezone.utc)
        card = gc.build_activity_card(_days("2026-06-12", [5, 5, 5, 5]), now)
        assert card["asset_path"].exists()
        assert "20 total contributions" in card["alt"]


class TestRenderActivityCard:
    def test_renders_image_at_expected_size(self) -> None:
        # Deterministic (no network): the timestamp is passed in, not read from
        # the clock — so this guards the render signature and canvas size.
        from datetime import datetime, timezone
        stats = gc.compute_activity_stats(_days("2026-06-12", [5, 5, 5, 5]),
                                          date(2026, 6, 15))
        img = gc.render_activity_card(stats, datetime(2026, 6, 15, 16, 45, tzinfo=timezone.utc))
        assert img.size == (gc.ACTIVITY_RENDER_W, gc.ACTIVITY_RENDER_H)
        assert img.mode == "RGBA"


class TestIsHttpUrl:
    def test_accepts_absolute_http_and_https(self) -> None:
        assert gc.is_http_url("https://example.com/post") is True
        assert gc.is_http_url("http://example.com/post") is True

    def test_rejects_other_schemes_and_relative_urls(self) -> None:
        # M1: feed permalinks land in README href attributes — anything that
        # isn't an absolute http(s) URL must be refused at ingestion.
        assert gc.is_http_url("javascript:alert(1)") is False
        assert gc.is_http_url("ftp://example.com/f") is False
        assert gc.is_http_url("/relative/path") is False
        assert gc.is_http_url("") is False


class TestBuildBlogCards:
    def test_skips_item_with_non_http_link(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # M1: a hostile feed <link> must never reach the generated HTML.
        feed = (
            "<rss><channel>"
            "<item><title>Bad</title><link>javascript:alert(1)</link>"
            "<description>d</description></item>"
            "<item><title>Good</title><link>https://example.com/good</link>"
            "<description>d</description></item>"
            "</channel></rss>"
        )
        monkeypatch.setattr(gc, "fetch_rss", lambda url: ET.fromstring(feed))
        monkeypatch.setattr(gc, "ASSETS_DIR", tmp_path)
        monkeypatch.setattr(gc, "og_image", lambda url: None)
        monkeypatch.setattr(gc, "render_card",
                            lambda *a, **k: Image.new("RGBA", (8, 8)))
        cards = gc.build_blog_cards()
        assert [c["url"] for c in cards] == ["https://example.com/good"]


class TestBuildMastoCards:
    def test_skips_item_with_non_http_link(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # M1: same ingestion guard as the blog builder, Mastodon side.
        feed = (
            '<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>'
            "<item><link>data:text/html,x</link>"
            "<description>a real caption</description></item>"
            "<item><link>https://infosec.exchange/@b/1</link>"
            "<description>another caption</description></item>"
            "</channel></rss>"
        )
        monkeypatch.setattr(gc, "fetch_rss", lambda url: ET.fromstring(feed))
        monkeypatch.setattr(gc, "ASSETS_DIR", tmp_path)
        monkeypatch.setattr(gc, "masto_hero", lambda *a: gc.MASTO_AVATAR)
        monkeypatch.setattr(gc, "render_card",
                            lambda *a, **k: Image.new("RGBA", (8, 8)))
        cards = gc.build_masto_cards()
        assert [c["url"] for c in cards] == ["https://infosec.exchange/@b/1"]


class TestFeedFetchCaps:
    """L2 — every fetch path is byte-capped, not just images/video."""

    def test_fetch_rss_caps_read_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, int | None] = {}

        def fake_fetch(url: str, *, max_bytes: int | None = None) -> bytes:
            seen["max_bytes"] = max_bytes
            return b"<rss/>"

        monkeypatch.setattr(gc, "fetch_url", fake_fetch)
        gc.fetch_rss("https://example.com/feed")
        assert seen["max_bytes"] == gc.FEED_MAX_BYTES

    def test_masto_card_lookup_caps_read_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, int | None] = {}

        def fake_fetch(url: str, *, max_bytes: int | None = None) -> bytes:
            seen["max_bytes"] = max_bytes
            return b'{"card": {"image": "https://cdn.example/img.png"}}'

        monkeypatch.setattr(gc, "fetch_url", fake_fetch)
        img = gc.masto_card_image("https://infosec.exchange/@b/123")
        assert img == "https://cdn.example/img.png"
        assert seen["max_bytes"] == gc.FEED_MAX_BYTES


class TestActivityToHtml:
    def test_emits_clickable_image_with_escaped_alt_and_width(self) -> None:
        card = gc.Card(asset_path=Path("assets/card.png"), rel_src="assets/activity_card.png?v=abc12345",
                       url="https://github.com/bjgreenberg",
                       alt='Activity "stats" & streaks')
        out = gc.activity_to_html(card)
        assert 'href="https://github.com/bjgreenberg"' in out
        assert f'width="{gc.ACTIVITY_DISPLAY_W}"' in out
        assert 'assets/activity_card.png?v=abc12345' in out
        assert "&quot;stats&quot; &amp; streaks" in out  # HTML-escaped
        assert out.startswith('<p align="center">')

    def test_escapes_quote_in_url_attribute(self) -> None:
        # M1: a URL containing `"` must not break out of the href attribute.
        card = gc.Card(asset_path=Path("assets/card.png"), rel_src="a.png",
                       url='https://example.com/y"onmouseover=x', alt="t")
        out = gc.activity_to_html(card)
        assert '/y"onmouseover' not in out
        assert '/y&quot;onmouseover' in out


class TestCardsToHtml:
    def test_emits_anchor_with_new_tab_and_alt(self) -> None:
        cards = [
            gc.Card(asset_path=Path("assets/card.png"), rel_src="assets/c1.png",
                    url="https://example.com/post", alt='A "quoted" title'),
        ]
        html_out = gc.cards_to_html(cards)
        assert 'href="https://example.com/post"' in html_out
        assert 'target="_blank"' in html_out
        assert 'rel="noopener noreferrer"' in html_out
        assert f'width="{gc.DISPLAY_W}"' in html_out
        # Alt text must be HTML-escaped to survive quotes.
        assert "&quot;quoted&quot;" in html_out

    def test_escapes_quote_in_url_attribute(self) -> None:
        # M1: a feed-supplied URL containing `"` must not break out of the
        # href attribute (HTML injection into the committed README).
        cards = [gc.Card(asset_path=Path("assets/card.png"), rel_src="a.png",
                         url='https://example.com/x"><img src=x>', alt="t")]
        html_out = gc.cards_to_html(cards)
        assert '/x"><img' not in html_out
        assert 'https://example.com/x&quot;&gt;&lt;img' in html_out

    def test_centers_with_p_tag(self) -> None:
        cards = [gc.Card(asset_path=Path("assets/card.png"), rel_src="a.png", url="u", alt="x")]
        assert gc.cards_to_html(cards).startswith('<p align="center">')


def _tiny_png() -> bytes:
    """A minimal valid PNG for exercising the image-fetch call path."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class TestHostGuard:
    """#2 — fetch_url must refuse non-public hosts (SSRF defense-in-depth).

    Attacker-influenceable URLs reach fetch_url (a shared article's og:image),
    so a target of 169.254.169.254 / an internal host must be refused before
    any socket is opened. Literal-IP checks resolve offline (no network).
    """

    def test_allows_public_ip(self) -> None:
        assert gc._host_is_public("8.8.8.8") is True

    def test_rejects_loopback_v4(self) -> None:
        assert gc._host_is_public("127.0.0.1") is False

    def test_rejects_private(self) -> None:
        assert gc._host_is_public("10.0.0.1") is False

    def test_rejects_cloud_metadata_link_local(self) -> None:
        assert gc._host_is_public("169.254.169.254") is False

    def test_rejects_loopback_v6(self) -> None:
        assert gc._host_is_public("::1") is False

    def test_rejects_unspecified(self) -> None:
        assert gc._host_is_public("0.0.0.0") is False

    def test_rejects_empty(self) -> None:
        assert gc._host_is_public("") is False

    def test_fetch_url_refuses_metadata_endpoint(self) -> None:
        with pytest.raises(ValueError):
            gc.fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_fetch_url_refuses_loopback(self) -> None:
        with pytest.raises(ValueError):
            gc.fetch_url("http://127.0.0.1:8080/admin")


class TestImageFetchSizeCap:
    """#1 — image downloads must pass an explicit byte cap to fetch_url.

    An uncapped fetch of an attacker-controlled og:image can OOM the runner
    (multi-GB body) before Pillow ever sees it. The video path already caps;
    the two image paths must too.
    """

    def test_fetch_photo_caps_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def rec(url: str, **kw: object) -> bytes:
            seen.update(kw)
            return _tiny_png()

        monkeypatch.setattr(gc, "fetch_url", rec)
        gc.fetch_photo("https://example.com/hero.jpg", 10, 10)
        assert seen.get("max_bytes") == gc.IMAGE_MAX_BYTES

    def test_usable_image_caps_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def rec(url: str, **kw: object) -> bytes:
            seen.update(kw)
            return _tiny_png()

        monkeypatch.setattr(gc, "fetch_url", rec)
        gc.usable_image("https://example.com/hero.jpg")
        assert seen.get("max_bytes") == gc.IMAGE_MAX_BYTES


class TestRedirectHandler:
    """#2 (redirect hop) — the SSRF guard also covers 30x redirects.

    A relative ``Location`` must resolve against the request URL (not be
    rejected as host-less), while a redirect that lands on a private/metadata
    host is refused.
    """

    def test_relative_location_resolves_and_is_allowed(self) -> None:
        h = gc._PublicOnlyRedirectHandler()
        req = urllib.request.Request("https://example.com/a/b")
        new = h.redirect_request(req, io.BytesIO(b""), 302, "Found", HTTPMessage(), "/c")
        assert new is not None
        assert new.full_url == "https://example.com/c"

    def test_redirect_to_metadata_host_is_refused(self) -> None:
        h = gc._PublicOnlyRedirectHandler()
        req = urllib.request.Request("https://example.com/")
        with pytest.raises(urllib.error.URLError):
            h.redirect_request(req, io.BytesIO(b""), 302, "Found", HTTPMessage(),
                               "http://169.254.169.254/latest/meta-data/")
