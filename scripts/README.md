# Profile README automation

Generates the **Latest from the Blog** and **Latest from Mastodon** sections of
the GitHub profile README (`../README.md`) as borderless, clickable image
"cards", refreshed daily by GitHub Actions.

---

## Why image cards instead of an HTML table

GitHub's markdown sanitizer **strips `border` and `style` attributes** from
`<table>` tags and applies its own cell borders, so a clean borderless card grid
is impossible with live HTML. It also strips `<map>`/`<area>`, ruling out image
maps.

The workaround: render each post to a **self-contained PNG** (featured image +
title/blurb baked in) and place three per row inside a `<p align="center">`. No
`<table>`, so **no borders**. Each card is wrapped in its own
`<a href="post-url">`, so every card is **independently clickable** and opens the
post in a new tab.

### Trade-off

The title/blurb text is rasterized into the image — it is **not selectable or
indexable** by search engines. The post title is carried in the `<img alt="">`
attribute for basic screen-reader accessibility.

---

## Prerequisites

- Python 3.12+
- Pillow (pinned in [`requirements.txt`](requirements.txt))
- A TrueType font:
  - **Ubuntu CI** — DejaVu (`fonts-dejavu-core`, preinstalled on `ubuntu-latest`)
  - **macOS (local)** — Arial (preinstalled)
  - Falls back to Pillow's bitmap font if neither is found (ugly but non-fatal)

## Setup

```bash
pip3 install -r scripts/requirements.txt
```

## Usage

```bash
# Regenerate cards + rewrite the README sections in place
python3 scripts/generate_cards.py

# Render cards but leave README.md untouched (inspect assets/ output)
python3 scripts/generate_cards.py --dry-run
```

Cards are written to `../assets/blog_card_{1..3}.png` and
`../assets/masto_card_{1..3}.png`. The script overwrites the same filenames each
run, so the README references are stable.

## How it runs in production

[`.github/workflows/blog-posts.yml`](../.github/workflows/blog-posts.yml) runs
daily at **08:00 UTC** (and on manual dispatch from the Actions tab). It installs
Pillow, runs the script, and commits any changed PNGs + README. The only secret
used is the automatically-provided `GITHUB_TOKEN` (no configuration required).

No cost: the profile repo is public, and GitHub Actions is free for public repos.

---

## Data sources

| Section | Feed | Image source |
|---|---|---|
| Blog | `https://briangreenberg.net/feed/` | First `<img>` in `<content:encoded>`; falls back to the post page's `og:image` meta tag |
| Mastodon | `https://infosec.exchange/@brian_greenberg.rss` | `<media:content>` attachment; falls back to the account avatar |

Mastodon link-share posts (a bare URL with no caption) are skipped. Emoji are
stripped from the baked card text because the bundled fonts have no color-emoji
glyphs (the link still points at the full original post).

---

## Files and modules

### `generate_cards.py`

| Function | Purpose |
|---|---|
| `fetch_url` | HTTP GET with a UA header; optional byte cap for `<head>` scraping |
| `fetch_rss` | Fetch + parse an RSS feed to an XML root element |
| `strip_tags` / `collapse_ws` | Clean HTML and whitespace from feed text |
| `strip_emoji` | Drop glyphs the fonts can't render (emoji/symbols) |
| `make_excerpt` | Word-boundary truncate with an ellipsis |
| `is_bare_url` | Detect caption-less Mastodon link shares (skipped) |
| `og_image` | Scrape a post page's `og:image` when RSS has no inline image |
| `_find_font` / `_fonts` | Cross-platform (Ubuntu/macOS) TrueType font loading |
| `fetch_photo` | Download + resize a hero image, or a neutral placeholder on failure |
| `_wrap` | Greedy pixel-width word wrap |
| `render_card` | Render one RGBA card (rounded corners, photo + text) |
| `build_blog_cards` / `build_masto_cards` | Per-feed card builders → `list[Card]` |
| `cards_to_html` | Borderless centered `<p>` of per-card `<a><img></a>` links |
| `update_section` | Replace content between `<!-- TAG:START/END -->` markers |
| `main` | Orchestrate both feeds; `--dry-run` supported |

`Card` is a `TypedDict` describing a rendered card (asset path, README `src`,
post URL, alt text).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Card text renders as boxes (tofu) | Font not found on the runner. Confirm `fonts-dejavu-core` is present; `_find_font` logs a warning when it falls back to the bitmap default. |
| A card shows a gray placeholder | The hero image URL failed to download. Check the post's `og:image` / `<media:content>` URL is reachable. |
| Blog card has no image | The post had no inline image **and** no `og:image`. Add a featured image to the post. |
| Cards in a row have uneven heights | Should not happen — height is fixed per section via the `*_LINES` constants. If you change those, both cards in a section must use the same values. |
| `pip install` fails on the runner | The pinned Pillow version may lack a wheel for the runner's Python. Bump `Pillow==` in `requirements.txt` to a version with a `cp312` wheel. |
| Emoji missing from card text | Expected — emoji are stripped (no color-glyph support). The full post still has them. |

## Known limitations

- Baked text is not selectable/searchable (see trade-off above).
- Cards use a fixed GitHub-dark palette; they look identical on GitHub's light
  and dark themes (a deliberate dark card either way) thanks to transparent
  rounded corners.
- Three 260px cards (~810px total) fit GitHub's README content width. If a row
  wraps, reduce `DISPLAY_W` in `generate_cards.py`.
- Daily runs commit ~6 small PNGs; filenames are reused so the working tree
  stays flat, but git history does accrue blobs over time.
