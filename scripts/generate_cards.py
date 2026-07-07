#!/usr/bin/env python3
"""Generate composite post-card images for the GitHub profile README.

This script powers the "GitHub Activity", "Featured Project", "Latest from
the Blog", and "Latest from Mastodon" sections of the profile README. For
each feed it builds up to three self-contained PNG "cards" (featured image +
title/text baked in); from one GraphQL contribution-calendar fetch it renders
two GitHub Activity cards — the streak/stats card (total/current/longest) and
a trailing-year contribution heatmap (GitHub's green squares) — and it bakes
a pin-style Featured Project card from live repo metadata. All are saved under
``assets/`` and the marked README sections are rewritten with a borderless
``<p>`` of per-card links.

Why a baked activity card instead of a third-party streak service:
    The previous ``streak-stats.demolab.com`` image (and self-hosting it on
    Vercel) proved unreliable — the shared demo host times out behind GitHub's
    camo proxy, and the project no longer ships a one-click Vercel config.
    Rendering the card here removes every external dependency: same approach,
    same daily bot, no service to break.

Why baked images instead of an HTML table:
    GitHub's markdown sanitizer strips ``border``/``style`` attributes from
    ``<table>`` and applies its own cell borders, so a clean borderless card
    grid is impossible with live HTML. Rendering each card to a PNG sidesteps
    this entirely while keeping every card independently clickable (each image
    is wrapped in its own ``<a href=post-url>``).

Trade-off:
    Title/blurb text is rasterized into the image, so it is not selectable or
    indexable. The ``alt`` attribute carries the title for basic accessibility.

Usage:
    python3 scripts/generate_cards.py            # update README in place
    python3 scripts/generate_cards.py --dry-run  # log what would change, write nothing

See scripts/README.md for full documentation.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import io
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess  # nosec B404 — used only with fixed args + absolute ffmpeg path, no shell
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405 — type hints only; parsing uses defusedxml
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring as safe_xml_fromstring
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ── Configuration ───────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
ASSETS_DIR = REPO_ROOT / "assets"

BLOG_FEED = "https://briangreenberg.net/feed/"
MASTO_FEED = "https://infosec.exchange/@brian_greenberg.rss"

# GitHub contribution data for the baked "GitHub Activity" card.
GITHUB_LOGIN = "bjgreenberg"
GITHUB_PROFILE_URL = "https://github.com/bjgreenberg"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

# Timezone for the activity card's "Updated …" stamp (Brian is Chicago-based).
DISPLAY_TZ = "America/Chicago"

# Mastodon video posts: pull a real hero frame with ffmpeg instead of relying
# on the instance poster (which is frame 0 — often a blank intro card).
VIDEO_FRAME_SECONDS = (1, 2, 4)      # seek points to try, in order
MAX_VIDEO_BYTES = 60_000_000         # safety cap on a downloaded attachment

# Safety cap on any fetched image body. Card heroes come from partly
# attacker-influenced URLs (a shared article's og:image), so an uncapped read
# could pull a multi-GB body and OOM the runner before Pillow decodes it.
IMAGE_MAX_BYTES = 20_000_000
# Bound decoded pixel count too (decompression bomb) rather than relying on
# Pillow's ~89 Mpx default — a card hero never needs more than this.
Image.MAX_IMAGE_PIXELS = 50_000_000

# Avatar used when a Mastodon post has no media attachment of its own.
MASTO_AVATAR = (
    "https://media.infosec.exchange/infosec.exchange/accounts/avatars/"
    "114/224/012/052/749/216/original/1e11732d1ff94efc.jpeg"
)

CARDS_PER_SECTION = 3
HTTP_TIMEOUT = 15
USER_AGENT = "bjgreenberg-readme-bot/1.0"

# Bytes to read when scraping a page's <head> for og:image. News sites
# (The Verge, Gizmodo, …) carry heavy <head> markup, so the 8 KB used for
# lightweight WP scraping is too small — the og:image meta sits past it.
OG_SCRAPE_BYTES = 40000

# RSS namespaces.
NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
NS_MEDIA = "{http://search.yahoo.com/mrss/}"

# ── Card geometry (rendered at 2× the display size for crisp text) ──────────

SCALE = 2
DISPLAY_W = 260                      # px width of each card in the README
RENDER_W = DISPLAY_W * SCALE         # 520
PHOTO_H = 290                        # photo band height (render px)
PAD = 28                             # inner padding (render px)
RADIUS = 24                          # corner radius (render px)
LINE_GAP = 6
TITLE_BLOCK_GAP = 10

# GitHub-dark palette (cards read as deliberate dark cards on either theme).
CARD_BG = (22, 27, 34, 255)          # #161b22
LINK_BLUE = (88, 166, 255)           # #58a6ff
MUTED_GRAY = (139, 148, 158)         # #8b949e
PLACEHOLDER_BG = (48, 54, 61)        # #30363d

# Max baked text lines.
BLOG_TITLE_LINES = 2
BLOG_BLURB_LINES = 3
MASTO_TEXT_LINES = 6

# ── Activity card geometry (rendered at 2× the display size) ─────────────────

ACTIVITY_DISPLAY_W = 760             # px width in the README (≈ the 3-card row width)
ACTIVITY_RENDER_W = ACTIVITY_DISPLAY_W * SCALE   # 1520
# The card layout is designed against a 900px-wide reference and scaled to the
# render width, so changing ACTIVITY_DISPLAY_W rescales type/ring/spacing
# uniformly (no stretched-canvas look). All the literal geometry inside
# render_activity_card is multiplied by ACTIVITY_SCALE.
_ACTIVITY_BASE_W = 900
ACTIVITY_SCALE = ACTIVITY_RENDER_W / _ACTIVITY_BASE_W
ACTIVITY_RENDER_H = round(332 * ACTIVITY_SCALE)  # display height ÷2; room for footer
RING_RADIUS = round(66 * ACTIVITY_SCALE)         # current-streak ring radius (render px)
RING_WIDTH = round(10 * ACTIVITY_SCALE)          # ring stroke width (render px)

# ── Contribution heatmap geometry (same 900px base design / scale) ──────────

HEATMAP_RENDER_H = round(214 * ACTIVITY_SCALE)
HEATMAP_WEEKS = 53                   # 52 full weeks + the current partial week

# GitHub dark-mode contribution palette, dimmest → brightest. Index = heatmap
# level 1–4; level 0 (no contributions) uses HEATMAP_EMPTY, which is a step
# lighter than CARD_BG so empty cells stay visible on the card.
HEATMAP_EMPTY = (33, 38, 45)         # #21262d
HEATMAP_LEVELS = (
    (14, 68, 41),                    # #0e4429
    (0, 109, 50),                    # #006d32
    (38, 166, 65),                   # #26a641
    (57, 211, 83),                   # #39d353
)

# ── Featured-project pin card (same 900px base design / scale) ──────────────
# Rendered daily by the same bot, so stars/forks/release stay current — a
# self-hosted stand-in for the github-readme-stats "pin" card.

FEATURED_OWNER = "bjgreenberg"
FEATURED_REPO = "senior-engineering-partner"
FEATURED_URL = f"https://github.com/{FEATURED_OWNER}/{FEATURED_REPO}"
FEATURED_RENDER_H = round(236 * ACTIVITY_SCALE)
FEATURED_DESC_LINES = 3
FEATURED_TEXT = (230, 237, 243)      # #e6edf3 — body text on the dark card

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("generate_cards")


# ── Typed structures ────────────────────────────────────────────────────────

class Card(TypedDict):
    """A single rendered card and the post it links to."""

    asset_path: Path   # where the PNG was written (under assets/)
    rel_src: str       # repo-relative path used in the README <img src>
    url: str           # post permalink (the <a href>)
    alt: str           # accessibility text (the post title / toot summary)


class ContribDay(TypedDict):
    """One day of the GitHub contribution calendar."""

    date: str          # ISO yyyy-mm-dd
    count: int          # contributions that day


class RepoMeta(TypedDict):
    """Live metadata for the featured-project pin card ("" when absent)."""

    description: str
    stars: int
    forks: int
    license: str       # SPDX id, e.g. "Apache-2.0"
    release: str       # latest release tag, e.g. "v1.16.2"
    language: str      # primary language name


class ActivityStats(TypedDict):
    """Computed GitHub contribution metrics for the activity card."""

    total: int                 # all-time total contributions
    since_year: int            # year of the earliest day in the calendar (0 if none)
    current_streak: int        # consecutive days up to today (grace for today)
    current_start: str         # ISO start date of the current streak ("" if none)
    current_end: str           # ISO end date of the current streak ("" if none)
    longest_streak: int        # longest run of consecutive contribution days
    longest_start: str         # ISO start date of the longest streak ("" if none)
    longest_end: str           # ISO end date of the longest streak ("" if none)


# ── Feed parsing helpers ────────────────────────────────────────────────────

def _host_is_public(host: str | None) -> bool:
    """True only if every address ``host`` resolves to is globally routable.

    Blocks SSRF to loopback/private/link-local/reserved ranges — notably the
    ``169.254.169.254`` cloud-metadata endpoint — for URLs derived from
    external, partly attacker-controlled data (a shared article's og:image). A
    literal IP resolves offline; a hostname is looked up once here.

    Residual (accepted for this daily first-party bot): the name is re-resolved
    at connect time, so a DNS-rebinding race could still swap in a private
    address between this check and the socket connect.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate scheme + host on every redirect hop.

    A public URL that 30x-redirects to ``http://169.254.169.254/`` would
    otherwise sail past the one-time pre-fetch check in ``fetch_url``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib already absolutizes newurl against req before calling this, but
        # resolve defensively so a relative/scheme-relative Location can never
        # slip through as a host-less URL that fails the public-host check
        # spuriously (a no-op when newurl is already absolute).
        abs_url = urllib.parse.urljoin(req.full_url, newurl)
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme not in ("http", "https") or not _host_is_public(parsed.hostname):
            raise urllib.error.URLError(f"Refusing redirect to non-public URL: {abs_url!r}")
        return super().redirect_request(req, fp, code, msg, headers, abs_url)


_OPENER = urllib.request.build_opener(_PublicOnlyRedirectHandler)


def fetch_url(url: str, *, max_bytes: int | None = None) -> bytes:
    """Fetch ``url`` and return the raw bytes.

    Args:
        url: HTTP(S) URL to fetch.
        max_bytes: If set, read at most this many bytes (used for cheap
            HTML <head> scraping).

    Returns:
        The response body as bytes.

    Raises:
        ValueError: if the URL scheme is not http/https (blocks file://, etc.)
            or the host resolves to a non-public address (SSRF guard).
        urllib.error.URLError / OSError on network failure.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Refusing non-HTTP(S) URL scheme: {parsed.scheme!r}")
    if not _host_is_public(parsed.hostname):
        raise ValueError(f"Refusing to fetch non-public host: {parsed.hostname!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310 — scheme + host validated above
        return resp.read(max_bytes) if max_bytes else resp.read()


def fetch_rss(url: str) -> ET.Element:
    """Fetch and parse an RSS feed into its XML root element.

    Uses defusedxml to guard against XML attacks (billion laughs, external
    entity expansion) even though the feeds are first-party.
    """
    return safe_xml_fromstring(fetch_url(url))


def strip_tags(text: str | None) -> str:
    """Remove HTML tags and unescape entities from feed text."""
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def collapse_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def strip_emoji(text: str) -> str:
    """Drop characters the bundled fonts cannot render (emoji, symbols).

    DejaVu/Arial have no color-emoji glyphs, so emoji would render as tofu
    boxes when baked into a card. We remove them from the *image* text only;
    the link still points at the full original post.
    """
    cleaned = "".join(ch for ch in text if ord(ch) < 0x2190 or 0x2C00 <= ord(ch) < 0x2E00)
    return collapse_ws(cleaned)


def make_excerpt(text: str, max_len: int = 180) -> str:
    """Trim ``text`` to ``max_len`` chars on a word boundary with an ellipsis."""
    text = collapse_ws(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rfind(" ")
    return (text[:cut] if cut > 0 else text[:max_len]) + "…"


def is_bare_url(text: str) -> bool:
    """True if ``text`` is just a single URL (a caption-less link share)."""
    return bool(re.match(r"^https?://\S+$", text.strip()))


def strip_photon(url: str) -> str:
    """Rewrite a Jetpack Photon CDN URL to its origin URL.

    Photon (``i0.wp.com``/``i1.wp.com``/…) edge nodes serving GitHub's
    runner region have returned persistent 404s for valid images (cached
    origin errors), while the origin itself serves them fine. We download
    the full image and do our own crop/resize anyway, so Photon's
    ``?resize=`` params buy nothing — fetch the origin directly.

    ``https://i0.wp.com/example.net/img.jpg?resize=1024%2C341&ssl=1``
    becomes ``https://example.net/img.jpg``. Non-Photon URLs pass through
    unchanged.
    """
    m = re.match(r"^https?://i\d\.wp\.com/([^?#]+)", url)
    return f"https://{m.group(1)}" if m else url


def asset_version(path: Path) -> str:
    """Short content hash of ``path``, used to cache-bust README image URLs.

    The card filenames are stable (``blog_card_1.png`` …), so without a
    version param the GitHub raw CDN, camo proxy, corporate web filters,
    and browsers all keep serving yesterday's bytes after a refresh. A
    content-hash query string gives every regenerated card a never-seen
    URL, making updates visible immediately.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def og_image(url: str) -> str | None:
    """Scrape a page's ``og:image`` (post permalink or linked article).

    The returned URL is HTML-unescaped: pages emit ``&amp;`` inside the
    ``content`` attribute, and an un-decoded ``?a=1&amp;b=2`` would reach
    GitHub's camo proxy with literal ``&amp;`` and fetch the wrong (or no)
    image.
    """
    try:
        head = fetch_url(url, max_bytes=OG_SCRAPE_BYTES).decode("utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        log.warning("og:image fetch failed for %s: %s", url, exc)
        return None
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pattern, head)
        if m:
            return html.unescape(m.group(1))
    return None


def media_kind(media: ET.Element) -> str:
    """Classify a ``media:content`` element as ``image``/``video``/``audio``.

    Mastodon tags every attachment with a ``medium`` attribute (and a MIME
    ``type``). The previous code ignored both and fed whatever URL it found
    to the image decoder — so a ``video/mp4`` attachment raised and the card
    fell back to a blank gray placeholder. Returns ``""`` for anything
    unrecognized.
    """
    medium = (media.get("medium") or "").lower()
    if medium in ("image", "video", "audio"):
        return medium
    mtype = (media.get("type") or "").lower()
    for kind in ("image", "video", "audio"):
        if mtype.startswith(f"{kind}/"):
            return kind
    return ""


def first_article_link(body_html: str) -> str | None:
    """Return the first outbound article URL in a toot's HTML body.

    Mastodon renders hashtags as ``class="mention hashtag"`` and @-mentions
    as ``class="u-url mention"`` — both contain ``mention``. A shared article
    link carries no such class. We therefore skip any anchor whose attributes
    mention ``mention`` and return the first remaining http(s) link, used to
    scrape the linked article's ``og:image`` as the card hero.
    """
    for m in re.finditer(r'<a\b([^>]*?)href=["\']([^"\']+)["\']([^>]*)>', body_html):
        attrs = (m.group(1) + m.group(3)).lower()
        if "mention" in attrs:
            continue
        href = html.unescape(m.group(2))
        if href.startswith(("http://", "https://")):
            return href
    return None


def _image_has_content(raw: bytes) -> bool:
    """True if image ``raw`` is not a near-flat single color.

    A blank poster / intro video frame is one flat color (e.g. ``#f2f2f2``)
    and carries no information. We measure the largest per-channel min→max
    spread; near-flat images have almost none. Returns False on decode error.
    """
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 — any decode error → unusable
        log.warning("Image content probe failed: %s", exc)
        return False
    return max(hi - lo for lo, hi in img.getextrema()) > 12


def usable_image(url: str) -> bool:
    """True if ``url`` downloads to an image with real visual content.

    A Mastodon video with no generated thumbnail yields a flat, single-color
    poster (observed: a 640×640 ``#f2f2f2`` square) — unusable, so the caller
    falls through to the next candidate. Returns False on any fetch error too.
    """
    try:
        raw = fetch_url(strip_photon(url), max_bytes=IMAGE_MAX_BYTES)
    except (OSError, ValueError) as exc:
        log.warning("Hero probe failed for %s: %s", url, exc)
        return False
    return _image_has_content(raw)


def masto_card_image(post_url: str) -> str | None:
    """Return the link-preview image the instance cached for a post, if any.

    When a toot shares a link, the Mastodon instance fetches the target
    server-side and caches a preview card whose image is rehosted on the
    instance's OWN media CDN (``media.<instance>``). We read it via the public
    ``/api/v1/statuses/{id}`` endpoint.

    This is far more reliable than scraping the linked news site directly from
    CI: news sites frequently block or rate-limit GitHub Actions' datacenter
    IPs (observed: a Gizmodo link resolved to its og:image locally but fell
    back to the avatar on the runner), whereas the instance CDN is the same
    host the avatar is already fetched from. The status id is the trailing
    numeric path segment of the permalink.
    """
    parts = urllib.parse.urlparse(post_url)
    m = re.search(r"/(\d+)/?$", parts.path)
    if not (m and parts.scheme in ("http", "https") and parts.netloc):
        return None
    api_url = f"{parts.scheme}://{parts.netloc}/api/v1/statuses/{m.group(1)}"
    try:
        status = json.loads(fetch_url(api_url))
    except (OSError, ValueError) as exc:
        log.warning("Mastodon card lookup failed for %s: %s", post_url, exc)
        return None
    card = status.get("card") if isinstance(status, dict) else None
    if isinstance(card, dict) and card.get("image"):
        return card["image"]
    return None


def extract_video_frame(video_url: str) -> bytes | None:
    """Extract a representative frame from a video attachment with ffmpeg.

    Mastodon's poster for a video is frame 0, which is frequently a blank
    intro card (observed: a solid ``#f2f2f2`` square that rendered an empty
    hero). We instead download the attachment and pull a frame a couple of
    seconds in — trying ``VIDEO_FRAME_SECONDS`` in order and returning the
    first one with real visual content.

    Returns the frame as PNG ``bytes``, or None when ffmpeg is unavailable,
    the download/decode fails, or every sampled frame is blank — in which case
    the caller falls back to the poster/preview/avatar chain. ffmpeg only ever
    touches local temp files we wrote (the URL is fetched via the scheme-
    validated ``fetch_url``), and is invoked with a fixed argument list and an
    absolute path — never a shell.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("ffmpeg not found — skipping video frame extraction.")
        return None
    try:
        raw = fetch_url(video_url, max_bytes=MAX_VIDEO_BYTES)
    except (OSError, ValueError) as exc:
        log.warning("Video download failed for %s: %s", video_url, exc)
        return None

    with tempfile.TemporaryDirectory() as tmp:
        video_path = os.path.join(tmp, "video")
        frame_path = os.path.join(tmp, "frame.png")
        Path(video_path).write_bytes(raw)
        for seconds in VIDEO_FRAME_SECONDS:
            try:
                subprocess.run(  # nosec B603 — fixed args, absolute path, no shell, local files only
                    [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                     "-ss", str(seconds), "-i", video_path,
                     "-frames:v", "1", frame_path],
                    timeout=HTTP_TIMEOUT, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("ffmpeg failed at %ss for %s: %s", seconds, video_url, exc)
                continue
            if not os.path.exists(frame_path):
                continue
            frame = Path(frame_path).read_bytes()
            if _image_has_content(frame):
                log.info("Extracted video frame at %ss for %s", seconds, video_url)
                return frame
    log.warning("No non-blank frame found for %s", video_url)
    return None


def masto_hero(item: ET.Element, post_url: str, body_html: str) -> str | bytes:
    """Choose the best hero for a Mastodon card (URL to fetch, or frame bytes).

    Priority, highest first:
        1. A native **image** attachment on the post.
        2. For a **video** attachment, a real frame extracted with ffmpeg
           (returned as bytes); failing that, the instance poster if it isn't
           blank (a README ``<img>`` cannot play video, so a still is right).
        3. For a **link** post, the instance's cached preview card, then the
           linked article's ``og:image``.
        4. Last resort (a rare pure-text post): the account avatar.
    """
    media_elems = item.findall(f"{NS_MEDIA}content")

    for media in media_elems:
        if media_kind(media) == "image" and media.get("url"):
            return media.get("url")

    video_urls = [m.get("url") for m in media_elems
                  if media_kind(m) == "video" and m.get("url")]
    if video_urls:
        for vurl in video_urls:
            frame = extract_video_frame(vurl)
            if frame:
                return frame
        poster = og_image(post_url)
        if poster and usable_image(poster):
            return poster

    # Link post: prefer the instance's own cached preview card (served from its
    # CDN, reliably reachable from CI). Fall back to scraping the linked
    # article's og:image only if the instance has no card for it.
    preview = masto_card_image(post_url)
    if preview:
        return preview

    link = first_article_link(body_html)
    if link:
        article_img = og_image(link)
        if article_img:
            return article_img

    return MASTO_AVATAR


# ── Image rendering ─────────────────────────────────────────────────────────

def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Return the first loadable TrueType font from ``candidates``.

    Tries platform-specific paths (Ubuntu CI uses DejaVu; macOS uses Arial),
    falling back to Pillow's bitmap default if none are present.
    """
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    log.warning("No TrueType font found in %s; using bitmap default.", candidates)
    return ImageFont.load_default(size)


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """Load (bold, regular) fonts for the current platform."""
    bold = _find_font(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Ubuntu CI
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
            "/Library/Fonts/Arial Bold.ttf",
        ],
        30,
    )
    regular = _find_font(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ],
        23,
    )
    return bold, regular


def fetch_photo(src: str | bytes, width: int, height: int) -> Image.Image:
    """Return a hero image cropped-to-fill the target box, or a placeholder.

    ``src`` is either an image URL to download, or already-decoded image
    ``bytes`` (e.g. an ffmpeg-extracted video frame). ``ImageOps.fit`` (center
    crop + resize) fills the card's hero band without distorting any aspect
    ratio.
    """
    try:
        raw = src if isinstance(src, bytes) else fetch_url(strip_photon(src), max_bytes=IMAGE_MAX_BYTES)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return ImageOps.fit(img, (width, height), method=Image.LANCZOS,
                            centering=(0.5, 0.5))
    except Exception as exc:  # noqa: BLE001 — any decode/network error → placeholder
        label = "<bytes>" if isinstance(src, bytes) else src
        log.warning("Photo fetch failed for %s: %s", label, exc)
        return Image.new("RGB", (width, height), PLACEHOLDER_BG)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> list[str]:
    """Greedy word-wrap ``text`` to fit ``max_w`` pixels per line."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_w:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_card(
    photo_src: str | bytes,
    primary: str,
    *,
    primary_color: tuple[int, int, int],
    primary_max_lines: int,
    secondary: str | None = None,
    secondary_max_lines: int = 0,
) -> Image.Image:
    """Render a single card to an RGBA image.

    Args:
        photo_src: Hero image URL to download, or pre-decoded image bytes.
        primary: Main text (blog title or toot text).
        primary_color: RGB color for the primary text.
        primary_max_lines: Max wrapped lines for the primary text.
        secondary: Optional secondary text (blog blurb); muted gray.
        secondary_max_lines: Max wrapped lines for the secondary text.

    Returns:
        An RGBA ``Image`` with transparent rounded corners so it composites
        cleanly on either GitHub theme.
    """
    bold, regular = _fonts()
    inner_w = RENDER_W - PAD * 2

    # Pre-wrap to compute required height.
    tmp = ImageDraw.Draw(Image.new("RGBA", (RENDER_W, 10)))
    primary_lines = _wrap(tmp, primary, bold, inner_w)[:primary_max_lines]
    secondary_lines = (
        _wrap(tmp, secondary, regular, inner_w)[:secondary_max_lines]
        if secondary else []
    )

    title_lh = bold.size + LINE_GAP
    blurb_lh = regular.size + LINE_GAP
    # Reserve height for the MAX line counts (not the actual wrapped count) so
    # every card in a section is the same height and the row stays flush.
    text_h = (
        PAD
        + primary_max_lines * title_lh
        + (TITLE_BLOCK_GAP + secondary_max_lines * blurb_lh if secondary else 0)
        + PAD
    )
    card_h = PHOTO_H + text_h

    card = Image.new("RGBA", (RENDER_W, card_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, RENDER_W, card_h], radius=RADIUS, fill=CARD_BG)

    # Hero photo with rounded top corners (square bottom blends into the card).
    photo = fetch_photo(photo_src, RENDER_W, PHOTO_H)
    mask = Image.new("L", (RENDER_W, PHOTO_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, RENDER_W, PHOTO_H + RADIUS], radius=RADIUS, fill=255
    )
    card.paste(photo, (0, 0), mask)

    # Text block.
    y = PHOTO_H + PAD
    for line in primary_lines:
        draw.text((PAD, y), line, font=bold, fill=primary_color)
        y += title_lh
    if secondary_lines:
        y += TITLE_BLOCK_GAP - LINE_GAP
        for line in secondary_lines:
            draw.text((PAD, y), line, font=regular, fill=MUTED_GRAY)
            y += blurb_lh

    return card


# ── Card builders per feed ──────────────────────────────────────────────────

def build_blog_cards() -> list[Card]:
    """Fetch the blog feed and render up to ``CARDS_PER_SECTION`` cards."""
    root = fetch_rss(BLOG_FEED)
    cards: list[Card] = []
    for item in root.findall(".//item"):
        if len(cards) >= CARDS_PER_SECTION:
            break
        title = collapse_ws(strip_tags(item.findtext("title")))
        url = (item.findtext("link") or "").strip()
        if not title or not url:
            continue

        blurb = strip_tags(item.findtext("description"))
        blurb = re.split(r"\s+The post\s+", blurb)[0].strip()  # drop WP boilerplate

        encoded = item.findtext(f"{NS_CONTENT}encoded") or ""
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', encoded)
        img_url = m.group(1) if m else og_image(url)
        if not img_url:
            log.warning("No image for blog post: %s", title)
            img_url = ""  # placeholder will render

        idx = len(cards) + 1
        path = ASSETS_DIR / f"blog_card_{idx}.png"
        card_img = render_card(
            img_url,
            title,
            primary_color=LINK_BLUE,
            primary_max_lines=BLOG_TITLE_LINES,
            secondary=make_excerpt(blurb),
            secondary_max_lines=BLOG_BLURB_LINES,
        )
        card_img.save(path)
        cards.append(Card(asset_path=path, rel_src=f"assets/{path.name}?v={asset_version(path)}",
                          url=url, alt=title))
        log.info("Blog card %d: %s", idx, title[:60])
    return cards


def build_masto_cards() -> list[Card]:
    """Fetch the Mastodon feed and render up to ``CARDS_PER_SECTION`` cards."""
    root = fetch_rss(MASTO_FEED)
    cards: list[Card] = []
    for item in root.findall(".//item"):
        if len(cards) >= CARDS_PER_SECTION:
            break
        url = (item.findtext("link") or "").strip()
        raw_desc = item.findtext("description") or ""
        text = collapse_ws(strip_tags(raw_desc))
        if not url or not text or is_bare_url(text):
            continue

        hero = masto_hero(item, url, raw_desc)

        baked = make_excerpt(strip_emoji(text)) or "View post"
        idx = len(cards) + 1
        path = ASSETS_DIR / f"masto_card_{idx}.png"
        card_img = render_card(
            hero,
            baked,
            primary_color=LINK_BLUE,
            primary_max_lines=MASTO_TEXT_LINES,
        )
        card_img.save(path)
        cards.append(Card(asset_path=path, rel_src=f"assets/{path.name}?v={asset_version(path)}",
                          url=url, alt=make_excerpt(text, 100)))
        log.info("Masto card %d: %s", idx, baked[:60])
    return cards


# ── GitHub activity card ─────────────────────────────────────────────────────

def github_token() -> str | None:
    """Read a GitHub API token from the environment.

    Prefers ``GH_TOKEN`` (an optional fine-grained PAT repo secret) and falls
    back to ``GITHUB_TOKEN`` (the token Actions injects automatically). Returns
    None when neither is set, so a local run without a token degrades to
    "skip the activity card" rather than crashing.
    """
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def _github_graphql(query: str, variables: dict[str, str], token: str) -> dict:
    """POST one GitHub GraphQL query and return the ``data`` payload."""
    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GITHUB_GRAPHQL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310 — constant HTTPS endpoint
        payload = json.loads(resp.read())
    if payload.get("errors"):
        raise ValueError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def fetch_contribution_days(login: str, token: str) -> list[ContribDay]:
    """Fetch the full contribution calendar for ``login`` via the GraphQL API.

    The ``contributionCalendar`` is capped at one year per query, so we read
    the account's ``createdAt`` and then loop year-by-year to assemble the
    complete day-by-day history (needed for an all-time longest streak). Days
    are returned sorted ascending by date.
    """
    def graphql(query: str, variables: dict[str, str]) -> dict:
        return _github_graphql(query, variables, token)["user"]

    created = graphql(
        "query($login:String!){ user(login:$login){ createdAt } }",
        {"login": login},
    )["createdAt"]
    start_year = int(created[:4])
    end_year = datetime.now(timezone.utc).year

    cal_query = (
        "query($login:String!,$from:DateTime!,$to:DateTime!){"
        " user(login:$login){ contributionsCollection(from:$from,to:$to){"
        " contributionCalendar{ weeks{ contributionDays{ date contributionCount } } } } } }"
    )
    days: list[ContribDay] = []
    for year in range(start_year, end_year + 1):
        data = graphql(cal_query, {
            "login": login,
            "from": f"{year}-01-01T00:00:00Z",
            "to": f"{year}-12-31T23:59:59Z",
        })
        weeks = data["contributionsCollection"]["contributionCalendar"]["weeks"]
        for week in weeks:
            for d in week["contributionDays"]:
                days.append(ContribDay(date=d["date"], count=d["contributionCount"]))
    days.sort(key=lambda d: d["date"])
    return days


def compute_activity_stats(days: list[ContribDay], today: date) -> ActivityStats:
    """Compute total, current, and longest contribution streaks.

    Streak rules mirror github-readme-streak-stats:
      * A streak is a run of consecutive calendar days each with >0 contributions.
      * The **current** streak ends today; if today has no contributions yet it
        is not counted as broken (grace period) — the streak through yesterday
        still stands. If the most recent contribution is older than yesterday,
        the current streak is 0.
    """
    # The current-year calendar includes future days (count 0 through Dec 31).
    # Drop anything after today so a trailing run of future zeros can't read as
    # a broken streak. ISO date strings sort chronologically, so string
    # comparison is safe here.
    days = [d for d in days if d["date"] <= today.isoformat()]

    stats = ActivityStats(
        total=sum(d["count"] for d in days),
        since_year=int(days[0]["date"][:4]) if days else 0,
        current_streak=0, current_start="", current_end="",
        longest_streak=0, longest_start="", longest_end="",
    )
    if not days:
        return stats

    # Longest streak: scan for the longest run of contribution days.
    run = 0
    run_start = ""
    for d in days:
        if d["count"] > 0:
            run += 1
            if run == 1:
                run_start = d["date"]
            if run > stats["longest_streak"]:
                stats["longest_streak"] = run
                stats["longest_start"] = run_start
                stats["longest_end"] = d["date"]
        else:
            run = 0

    # Current streak: walk backward from the last day. A trailing zero is only
    # forgiven when it is *today* (the day isn't over yet).
    i = len(days) - 1
    if days[i]["count"] == 0 and days[i]["date"] == today.isoformat():
        i -= 1
    end = i
    while i >= 0 and days[i]["count"] > 0:
        i -= 1
    if end >= 0 and i < end:
        stats["current_streak"] = end - i
        stats["current_start"] = days[i + 1]["date"]
        stats["current_end"] = days[end]["date"]
    return stats


def _fmt_date(iso: str) -> str:
    """Format an ISO date as e.g. ``Sep 23, 2010`` (no leading zero on day)."""
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _fmt_range(start: str, end: str, *, current: bool) -> str:
    """Human range for a streak; current streaks end in ``Present``."""
    if not start:
        return "—"
    left = _fmt_date(start)
    right = "Present" if current else _fmt_date(end)
    return f"{left} – {right}"


def _activity_stamp(generated_at: datetime) -> str:
    """Format the "Updated …" footer in Chicago local time (CST/CDT).

    ``generated_at`` is a UTC-aware datetime; it is converted to
    ``DISPLAY_TZ`` and rendered 12-hour with the live zone abbreviation
    (e.g. ``Updated Jun 15, 2026 · 8:32 PM CDT``). Falls back to the input's
    own zone if the tz database is unavailable.
    """
    try:
        local = generated_at.astimezone(ZoneInfo(DISPLAY_TZ))
    except Exception as exc:  # noqa: BLE001 — missing tzdata → show source zone
        log.warning("Timezone %s unavailable: %s", DISPLAY_TZ, exc)
        local = generated_at
    hour = local.strftime("%I").lstrip("0") or "12"
    zone = local.strftime("%Z") or "UTC"
    return (f"Updated {local.strftime('%b')} {local.day}, {local.year} · "
            f"{hour}:{local.strftime('%M')} {local.strftime('%p')} {zone}")


def _draw_centered(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str,
                   font: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> None:
    """Draw ``text`` horizontally centered on ``cx`` at baseline-top ``y``."""
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def render_activity_card(stats: ActivityStats, generated_at: datetime) -> Image.Image:
    """Render the three-panel GitHub activity card (total / current / longest).

    Uses the same GitHub-dark palette as the blog/Mastodon cards so the whole
    README reads as one consistent set of dark cards on either theme. A small
    "Updated …" stamp (``generated_at``, in UTC) is drawn along the bottom so
    viewers can see the card is live and how fresh it is.
    """
    sc = ACTIVITY_SCALE

    def s(v: float) -> int:
        """Scale a base-design (900px-wide) measurement to the render width."""
        return round(v * sc)

    big = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"], s(52))
    label = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"], s(26))
    small = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"], s(20))
    footer_font = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"], s(16))

    w, h = ACTIVITY_RENDER_W, ACTIVITY_RENDER_H
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, w, h], radius=s(RADIUS), fill=CARD_BG)

    col = w // 3
    centers = (col // 2, w // 2, w - col // 2)
    # Column dividers (kept short; the footer sits below them).
    for x in (col, col * 2):
        draw.line([(x, s(50)), (x, s(250))], fill=MUTED_GRAY, width=1)

    # Left: total contributions.
    _draw_centered(draw, centers[0], s(70), f"{stats['total']:,}", big, LINK_BLUE)
    _draw_centered(draw, centers[0], s(150), "Total Contributions", label, LINK_BLUE)
    since = f"Since {stats['since_year']}" if stats["since_year"] else "All time"
    _draw_centered(draw, centers[0], s(200), since, small, MUTED_GRAY)

    # Middle: current streak inside a ring.
    cx, cy = centers[1], s(110)
    draw.ellipse([cx - RING_RADIUS, cy - RING_RADIUS, cx + RING_RADIUS, cy + RING_RADIUS],
                 outline=LINK_BLUE, width=RING_WIDTH)
    num = str(stats["current_streak"])
    nb = draw.textbbox((0, 0), num, font=big)
    draw.text((cx - (nb[2] - nb[0]) / 2, cy - (nb[3] - nb[1]) / 2 - nb[1]), num,
              font=big, fill=(230, 237, 243))
    _draw_centered(draw, cx, cy + RING_RADIUS + s(18), "Current Streak", label, LINK_BLUE)
    _draw_centered(draw, cx, cy + RING_RADIUS + s(56),
                   _fmt_range(stats["current_start"], stats["current_end"], current=True),
                   small, MUTED_GRAY)

    # Right: longest streak.
    _draw_centered(draw, centers[2], s(70), str(stats["longest_streak"]), big, LINK_BLUE)
    _draw_centered(draw, centers[2], s(150), "Longest Streak", label, LINK_BLUE)
    _draw_centered(draw, centers[2], s(200),
                   _fmt_range(stats["longest_start"], stats["longest_end"], current=False),
                   small, MUTED_GRAY)

    # Footer: small "Updated …" stamp (Chicago local time), centered.
    _draw_centered(draw, w // 2, h - s(38), _activity_stamp(generated_at),
                   footer_font, MUTED_GRAY)
    return card


def build_activity_card(days: list[ContribDay], now: datetime) -> Card:
    """Render the streak/stats card from pre-fetched contribution days.

    The GraphQL fetch lives in ``main`` so the heatmap card can reuse the same
    calendar without a second round of year-by-year queries.
    """
    stats = compute_activity_stats(days, now.date())
    path = ASSETS_DIR / "activity_card.png"
    render_activity_card(stats, now).save(path)
    alt = (f"GitHub activity — {stats['total']:,} total contributions, "
           f"{stats['current_streak']}-day current streak, "
           f"{stats['longest_streak']}-day longest streak")
    log.info("Activity card: %s", alt)
    return Card(asset_path=path, rel_src=f"assets/{path.name}?v={asset_version(path)}",
                url=GITHUB_PROFILE_URL, alt=alt)


def activity_to_html(card: Card) -> str:
    """Render a centered, clickable full-width card image link (760px row)."""
    return (
        '<p align="center">\n'
        f'  <a href="{card["url"]}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{card["rel_src"]}" width="{ACTIVITY_DISPLAY_W}" '
        f'alt="{html.escape(card["alt"], quote=True)}"/></a>\n'
        '</p>'
    )


# ── Contribution heatmap card ────────────────────────────────────────────────

def heatmap_start(today: date) -> date:
    """The Sunday that opens the trailing-year heatmap grid.

    Walks back 364 days and then snaps to the preceding Sunday (GitHub's
    calendar weeks run Sunday–Saturday), so the grid is always 53 columns:
    52 full weeks plus the current, possibly partial, one.
    """
    start = today - timedelta(days=364)
    return start - timedelta(days=(start.weekday() + 1) % 7)


def heatmap_level(count: int, peak: int) -> int:
    """Bucket a day's contribution count into intensity levels 0–4.

    Levels are quarters of the window's busiest day (``peak``), mirroring the
    look of GitHub's own heatmap: 0 = no contributions, 4 = at (or clamped
    above) the peak.
    """
    if count <= 0 or peak <= 0:
        return 0
    return min(4, math.ceil(count * 4 / peak))


def build_heatmap_grid(days: list[ContribDay], today: date) -> list[list[int | None]]:
    """Trailing-year grid of daily counts: ``grid[week][row]``, rows Sun–Sat.

    Cells after ``today`` (the tail of the final, partial week) are None so
    the renderer leaves them blank; dates missing from the calendar count 0.
    """
    counts = {d["date"]: d["count"] for d in days}
    start = heatmap_start(today)
    n_weeks = (today - start).days // 7 + 1
    grid: list[list[int | None]] = []
    for week in range(n_weeks):
        col: list[int | None] = []
        for row in range(7):
            day = start + timedelta(days=week * 7 + row)
            col.append(None if day > today else counts.get(day.isoformat(), 0))
        grid.append(col)
    return grid


def month_label_columns(start: date, n_weeks: int) -> list[tuple[int, str]]:
    """``(column, "Jan")`` pairs marking where a new month begins.

    A label lands on the first column whose Sunday falls in a new month. The
    leading label is dropped when the next one is fewer than three columns
    away — a sliver of a 13th month at the left edge would otherwise overlap
    its neighbour.
    """
    labels: list[tuple[int, str]] = []
    prev_month = 0
    for week in range(n_weeks):
        sunday = start + timedelta(days=week * 7)
        if sunday.month != prev_month:
            labels.append((week, sunday.strftime("%b")))
            prev_month = sunday.month
    if len(labels) >= 2 and labels[1][0] - labels[0][0] < 3:
        labels = labels[1:]
    return labels


def render_heatmap_card(grid: list[list[int | None]], start: date) -> Image.Image:
    """Render the trailing-year contribution heatmap (GitHub's green squares).

    Same GitHub-dark card chrome and 900px base design as the activity card,
    so the two stack as one visual unit in the README. Month labels run along
    the top, Mon/Wed/Fri gutter labels down the left, and a Less→More legend
    sits bottom-right.
    """
    sc = ACTIVITY_SCALE

    def s(v: float) -> int:
        """Scale a base-design (900px-wide) measurement to the render width."""
        return round(v * sc)

    label_font = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"], s(18))

    w, h = ACTIVITY_RENDER_W, HEATMAP_RENDER_H
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, w, h], radius=s(RADIUS), fill=CARD_BG)

    # Grid geometry (base-design units): weekday gutter left of the cells,
    # month labels above, legend below.
    gutter = 44
    grid_x0, grid_y0 = PAD + gutter, 56
    step = (_ACTIVITY_BASE_W - 2 * PAD - gutter) / HEATMAP_WEEKS  # ≈ 15.2
    cell = step - 2.5

    peak = max((c for col in grid for c in col if c), default=0)

    for week, name in month_label_columns(start, len(grid)):
        draw.text((s(grid_x0 + week * step), s(28)), name,
                  font=label_font, fill=MUTED_GRAY)

    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        draw.text((s(PAD), s(grid_y0 + row * step)), name,
                  font=label_font, fill=MUTED_GRAY)

    for week, col in enumerate(grid):
        for row, count in enumerate(col):
            if count is None:
                continue
            level = heatmap_level(count, peak)
            fill = HEATMAP_EMPTY if level == 0 else HEATMAP_LEVELS[level - 1]
            x, y = s(grid_x0 + week * step), s(grid_y0 + row * step)
            draw.rounded_rectangle([x, y, x + s(cell), y + s(cell)],
                                   radius=s(3), fill=fill)

    # Legend, right-aligned under the grid: Less ▢▢▢▢▢ More.
    ly = grid_y0 + 7 * step + 14
    gap = s(8)
    x_cursor = s(_ACTIVITY_BASE_W - PAD) - round(draw.textlength("More", font=label_font))
    draw.text((x_cursor, s(ly)), "More", font=label_font, fill=MUTED_GRAY)
    for i in range(4, -1, -1):
        x_cursor -= gap + s(cell)
        fill = HEATMAP_EMPTY if i == 0 else HEATMAP_LEVELS[i - 1]
        draw.rounded_rectangle([x_cursor, s(ly + 2), x_cursor + s(cell), s(ly + 2) + s(cell)],
                               radius=s(3), fill=fill)
    x_cursor -= gap + round(draw.textlength("Less", font=label_font))
    draw.text((x_cursor, s(ly)), "Less", font=label_font, fill=MUTED_GRAY)
    return card


def build_heatmap_card(days: list[ContribDay], today: date) -> Card:
    """Render the heatmap card from pre-fetched contribution days."""
    grid = build_heatmap_grid(days, today)
    year_total = sum(c for col in grid for c in col if c)
    path = ASSETS_DIR / "contrib_heatmap.png"
    render_heatmap_card(grid, heatmap_start(today)).save(path)
    alt = f"Contribution heatmap — {year_total:,} contributions in the past year"
    log.info("Heatmap card: %s", alt)
    return Card(asset_path=path, rel_src=f"assets/{path.name}?v={asset_version(path)}",
                url=GITHUB_PROFILE_URL, alt=alt)


# ── Featured-project pin card ────────────────────────────────────────────────

def fetch_repo_meta(owner: str, name: str, token: str) -> RepoMeta:
    """Fetch the featured repo's live metadata via one GraphQL query."""
    repo = _github_graphql(
        "query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){"
        " description stargazerCount forkCount licenseInfo{spdxId}"
        " latestRelease{tagName} primaryLanguage{name} } }",
        {"owner": owner, "name": name}, token)["repository"]
    return RepoMeta(
        description=repo["description"] or "",
        stars=repo["stargazerCount"],
        forks=repo["forkCount"],
        license=(repo["licenseInfo"] or {}).get("spdxId") or "",
        release=(repo["latestRelease"] or {}).get("tagName") or "",
        language=(repo["primaryLanguage"] or {}).get("name") or "",
    )


def featured_meta_line(meta: RepoMeta) -> str:
    """Compose the card's metadata row, skipping absent fields.

    E.g. ``Python · Apache-2.0 · v1.16.2 · 98 stars · 13 forks``. Stars and
    forks are always shown (a zero is honest); the rest only when present.
    """
    parts = [meta["language"], meta["license"], meta["release"],
             f"{meta['stars']:,} stars", f"{meta['forks']:,} forks"]
    return " · ".join(p for p in parts if p)


def render_featured_card(meta: RepoMeta) -> Image.Image:
    """Render the featured-project pin card (name, description, meta row)."""
    sc = ACTIVITY_SCALE

    def s(v: float) -> int:
        """Scale a base-design (900px-wide) measurement to the render width."""
        return round(v * sc)

    title_font = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"], s(30))
    body_font = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"], s(22))
    meta_font = _find_font(
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"], s(20))

    w, h = ACTIVITY_RENDER_W, FEATURED_RENDER_H
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, w, h], radius=s(RADIUS), fill=CARD_BG)

    draw.text((s(PAD), s(26)), f"{FEATURED_OWNER}/{FEATURED_REPO}",
              font=title_font, fill=LINK_BLUE)

    lines = _wrap(draw, meta["description"], body_font,
                  w - 2 * s(PAD))[:FEATURED_DESC_LINES]
    y = 76
    for line in lines:
        draw.text((s(PAD), s(y)), line, font=body_font, fill=FEATURED_TEXT)
        y += 30

    draw.text((s(PAD), s(FEATURED_RENDER_H / sc - 48)), featured_meta_line(meta),
              font=meta_font, fill=MUTED_GRAY)
    return card


def build_featured_card(token: str) -> Card:
    """Fetch the featured repo's metadata, render its pin card, return it."""
    meta = fetch_repo_meta(FEATURED_OWNER, FEATURED_REPO, token)
    path = ASSETS_DIR / "featured_card.png"
    render_featured_card(meta).save(path)
    alt = (f"Featured project — {FEATURED_OWNER}/{FEATURED_REPO}: "
           f"{featured_meta_line(meta)}")
    log.info("Featured card: %s", alt)
    return Card(asset_path=path, rel_src=f"assets/{path.name}?v={asset_version(path)}",
                url=FEATURED_URL, alt=alt)


# ── README assembly ─────────────────────────────────────────────────────────

def cards_to_html(cards: list[Card]) -> str:
    """Render a borderless, centered row of per-card image links."""
    anchors = [
        f'<a href="{c["url"]}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{c["rel_src"]}" width="{DISPLAY_W}" '
        f'alt="{html.escape(c["alt"], quote=True)}"/></a>'
        for c in cards
    ]
    return '<p align="center">\n  ' + "\n  ".join(anchors) + "\n</p>"


def update_section(readme: str, tag: str, content: str) -> str:
    """Replace the body between ``<!-- TAG:START -->`` / ``:END -->`` markers."""
    return re.sub(
        rf"<!-- {tag}:START -->.*?<!-- {tag}:END -->",
        f"<!-- {tag}:START -->\n{content}\n<!-- {tag}:END -->",
        readme,
        flags=re.DOTALL,
    )


def main() -> int:
    """Generate cards for both feeds and update the README. Returns exit code."""
    parser = argparse.ArgumentParser(description="Generate README post cards.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render cards but do not write README.md")
    args = parser.parse_args()

    ASSETS_DIR.mkdir(exist_ok=True)
    readme = README_PATH.read_text()

    # Each feed is independent: one failing must not wipe the other's section.
    try:
        blog = build_blog_cards()
        if blog:
            readme = update_section(readme, "BLOG-POST-LIST", cards_to_html(blog))
    except Exception as exc:  # noqa: BLE001
        log.error("Blog section failed: %s", exc)

    try:
        masto = build_masto_cards()
        if masto:
            readme = update_section(readme, "MASTODON-POST-LIST", cards_to_html(masto))
    except Exception as exc:  # noqa: BLE001
        log.error("Mastodon section failed: %s", exc)

    token = github_token()
    if not token:
        log.warning("No GH_TOKEN/GITHUB_TOKEN set — skipping GitHub cards.")

    if token:
        try:
            days = fetch_contribution_days(GITHUB_LOGIN, token)
            now = datetime.now(timezone.utc)
            readme = update_section(readme, "ACTIVITY-CARD",
                                    activity_to_html(build_activity_card(days, now)))
            readme = update_section(readme, "CONTRIB-HEATMAP",
                                    activity_to_html(build_heatmap_card(days, now.date())))
        except Exception as exc:  # noqa: BLE001
            log.error("GitHub activity section failed: %s", exc)

        try:
            readme = update_section(readme, "FEATURED-PROJECT",
                                    activity_to_html(build_featured_card(token)))
        except Exception as exc:  # noqa: BLE001
            log.error("Featured-project section failed: %s", exc)

    if args.dry_run:
        log.info("Dry run — README not written.")
        return 0

    README_PATH.write_text(readme)
    log.info("README updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
