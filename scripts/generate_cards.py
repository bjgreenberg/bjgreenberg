#!/usr/bin/env python3
"""Generate composite post-card images for the GitHub profile README.

This script powers the "Latest from the Blog" and "Latest from Mastodon"
sections of the profile README. For each feed it builds up to three
self-contained PNG "cards" (featured image + title/text baked in), saves
them under ``assets/``, and rewrites the marked README sections with a
borderless ``<p>`` of per-card links.

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
import html
import io
import logging
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405 — type hints only; parsing uses defusedxml
from pathlib import Path
from typing import TypedDict

from defusedxml.ElementTree import fromstring as safe_xml_fromstring
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ── Configuration ───────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
ASSETS_DIR = REPO_ROOT / "assets"

BLOG_FEED = "https://briangreenberg.net/feed/"
MASTO_FEED = "https://infosec.exchange/@brian_greenberg.rss"

# Avatar used when a Mastodon post has no media attachment of its own.
MASTO_AVATAR = (
    "https://media.infosec.exchange/infosec.exchange/accounts/avatars/"
    "114/224/012/052/749/216/original/1e11732d1ff94efc.jpeg"
)

CARDS_PER_SECTION = 3
HTTP_TIMEOUT = 15
USER_AGENT = "bjgreenberg-readme-bot/1.0"

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

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("generate_cards")


# ── Typed structures ────────────────────────────────────────────────────────

class Card(TypedDict):
    """A single rendered card and the post it links to."""

    asset_path: Path   # where the PNG was written (under assets/)
    rel_src: str       # repo-relative path used in the README <img src>
    url: str           # post permalink (the <a href>)
    alt: str           # accessibility text (the post title / toot summary)


# ── Feed parsing helpers ────────────────────────────────────────────────────

def fetch_url(url: str, *, max_bytes: int | None = None) -> bytes:
    """Fetch ``url`` and return the raw bytes.

    Args:
        url: HTTP(S) URL to fetch.
        max_bytes: If set, read at most this many bytes (used for cheap
            HTML <head> scraping).

    Returns:
        The response body as bytes.

    Raises:
        ValueError: if the URL scheme is not http/https (blocks file://, etc.).
        urllib.error.URLError / OSError on network failure.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"Refusing non-HTTP(S) URL scheme: {scheme!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310 — scheme validated above
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


def og_image(url: str) -> str | None:
    """Scrape a post page's ``og:image`` when the RSS body has no inline image."""
    try:
        head = fetch_url(url, max_bytes=8000).decode("utf-8", errors="replace")
    except OSError as exc:
        log.warning("og:image fetch failed for %s: %s", url, exc)
        return None
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pattern, head)
        if m:
            return m.group(1)
    return None


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


def fetch_photo(url: str, width: int, height: int) -> Image.Image:
    """Fetch a post image cropped-to-fill the target box, or a placeholder.

    Uses ``ImageOps.fit`` (center crop + resize) so source images of any
    aspect ratio fill the card's hero band without distortion — the previous
    plain ``resize`` stretched non-matching images.
    """
    try:
        raw = fetch_url(strip_photon(url))
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return ImageOps.fit(img, (width, height), method=Image.LANCZOS,
                            centering=(0.5, 0.5))
    except Exception as exc:  # noqa: BLE001 — any decode/network error → placeholder
        log.warning("Photo fetch failed for %s: %s", url, exc)
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
    photo_url: str,
    primary: str,
    *,
    primary_color: tuple[int, int, int],
    primary_max_lines: int,
    secondary: str | None = None,
    secondary_max_lines: int = 0,
) -> Image.Image:
    """Render a single card to an RGBA image.

    Args:
        photo_url: URL of the card's hero image.
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
    photo = fetch_photo(photo_url, RENDER_W, PHOTO_H)
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
        cards.append(Card(asset_path=path, rel_src=f"assets/{path.name}",
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
        text = collapse_ws(strip_tags(item.findtext("description")))
        if not url or not text or is_bare_url(text):
            continue

        media = item.find(f"{NS_MEDIA}content")
        img_url = media.get("url") if media is not None else MASTO_AVATAR

        baked = make_excerpt(strip_emoji(text)) or "View post"
        idx = len(cards) + 1
        path = ASSETS_DIR / f"masto_card_{idx}.png"
        card_img = render_card(
            img_url,
            baked,
            primary_color=LINK_BLUE,
            primary_max_lines=MASTO_TEXT_LINES,
        )
        card_img.save(path)
        cards.append(Card(asset_path=path, rel_src=f"assets/{path.name}",
                          url=url, alt=make_excerpt(text, 100)))
        log.info("Masto card %d: %s", idx, baked[:60])
    return cards


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

    if args.dry_run:
        log.info("Dry run — README not written.")
        return 0

    README_PATH.write_text(readme)
    log.info("README updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
