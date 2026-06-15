# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## 2026-06-15 (2)

### Fixed
- `generate_cards.py`: Mastodon **link** posts now pull their hero from the instance's cached preview card (`masto_card_image` reads `card.image` from `/api/v1/statuses/{id}`) instead of scraping the linked news site directly. After the earlier same-day fix went live, a Gizmodo-linked card resolved to the article image **locally** but fell back to the avatar **on the CI runner** — news sites routinely block or rate-limit GitHub Actions' datacenter IPs. The preview-card image is rehosted on `media.infosec.exchange` (the same CDN the avatar loads from), so it's reliably reachable from CI. Direct `og:image` article scraping is kept only as a fallback when the instance has no card. 6 new pytest cases (56 total); bandit clean

## 2026-06-15

### Fixed
- `generate_cards.py`: Mastodon cards picked the wrong hero image, so on the profile they showed up wrong — a video post rendered a **blank gray box** and several text/link posts all showed the **same account avatar**. Root cause: `build_masto_cards` blindly took the first `<media:content>` URL and ignored its `medium`/`type`. A `video/mp4` attachment was fed to the image decoder (→ placeholder), and any post without a media attachment fell straight back to the single hardcoded avatar (→ identical, off-topic heroes). Replaced the one-liner with `masto_hero_url`, which selects by priority: a native **image** attachment → for a **video**, the post's `og:image` poster frame (a README `<img>` can't play video) → for a **link** post, the **linked article's** `og:image` → account avatar as last resort. New helpers `media_kind` (classify `image`/`video`/`audio`) and `first_article_link` (first outbound article URL, skipping Mastodon mention/hashtag anchors)
- `generate_cards.py`: `usable_image` rejects a flat single-color hero so a card never renders blank. Mastodon serves a blank `#f2f2f2` poster for a video with no real thumbnail; that case now falls through to the article link, then the avatar
- `generate_cards.py`: `og_image` now HTML-unescapes the scraped URL (news-site og:images carry `&amp;` in the query string, which would otherwise reach GitHub's camo proxy literally and fetch the wrong/no image) and reads up to 40 KB of `<head>` (was 8 KB — too small for heavy news-site markup where the `og:image` meta sits past the cutoff)
- 20 new pytest cases (50 total) covering `media_kind`, `first_article_link`, `usable_image`, and the full `masto_hero_url` priority chain; bandit clean

## 2026-06-12

### Added
- `generate_cards.py`: `asset_version()` — card `<img src>` URLs now carry a content-hash query param (`assets/blog_card_1.png?v=<sha256[:8]>`). The filenames are stable, so previously every cache layer (GitHub raw CDN `max-age=300`, camo, corporate web filters, browsers) kept serving stale cards after a refresh — observed when the 2026-06-12 Photon fix landed but the profile still showed gray placeholders through a hard refresh. New bytes now always get a never-before-seen URL; unchanged bytes keep the same URL so a no-op regeneration doesn't churn the README. 3 new pytest cases

### Fixed
- `generate_cards.py`: the first two blog cards rendered with the gray placeholder because their hero images are served through Jetpack's Photon CDN (`i0.wp.com`), whose edge nodes fronting GitHub's runner region return persistent 404s for those URLs (cached origin errors) — the same URLs return 200 from everywhere else, and the origin images were never missing. Added `strip_photon()` (applied in `fetch_photo`, the single chokepoint for all card images): Photon URLs are rewritten to their origin form with the `?resize=…&ssl=1` params dropped — we download the full image and do our own Pillow crop anyway. Non-Photon hosts pass through unchanged. Covered by 7 new pytest cases (positive, negative, and lookalike-host)
- `blog-posts.yml`: the daily "Update README — latest posts" job committed the refreshed post cards on the runner but never pushed — `git push` was absent from every version of the workflow since the custom-Python rewrite (`c8e1b25`), so each day's commit died with the ephemeral runner and the run still reported success. The README's Mastodon/blog cards had been frozen at the last manually-committed state (~2026-06-09). Added `git push` after the commit; a push failure now fails the step instead of vanishing silently.

---

## 2026-06-10

### Added
- `ci`: GitHub Actions CI workflow (`test` job) — pytest + bandit on every PR and push to `main` (including daily bot commits), Python 3.12 to match the bot runtime. Non-gating safety net; rationale in `scripts/README.md`.

---

## 2026-06-08

### Added
- Hero banner (`assets/GitHub2025.png`, 1584×396) at the top of the README, wrapped in a centered responsive `<p>` with alt text
- `assets/GitHub2026.png` kept in the repo for the upcoming 2026 rebrand; a `HERO BANNER` comment marks where to swap it in

### Changed
- DePaul University Faculty badge now carries the DePaul crest as its icon. Cropped the crest emblem out of the full `DePaul Logo Transparent.png` wordmark (the white "DEPAUL UNIVERSITY" lettering is illegible at a 14px badge logo size, so only the crest is used) and embedded it as a percent-encoded `logo=data:image/png;base64,…` on the existing DePaul-blue (`#005EB8`) badge — same technique as the LinkedIn/Forbes badges. White-on-transparent crest stays visible in both light and dark GitHub themes because it sits on the blue badge fill rather than the page background. Source crest saved as `assets/depaul-crest.png` (149×155) for future regeneration

### Fixed
- DePaul badge rendered as a broken image on the live profile: the first attempt embedded the crest as an optimized RGBA PNG, producing a 4,193-char badge URL. shields.io serves it fine directly, but GitHub's camo image proxy will not proxy URLs that long (the working LinkedIn/Forbes badges are ~1,100–1,500 chars). Fixed by encoding the 38×40 crest as a 16-color paletted PNG instead of RGBA, which dropped the badge URL to 1,545 chars with no visible quality loss — back in camo's working range

---

## 2026-06-05 (patch 7)

### Added
- Restored LinkedIn and Forbes badge icons via embedded base64 SVG `logo=data:image/svg+xml;base64,…`. shields.io refuses to serve `logo=linkedin` (its own trademark policy) and Forbes isn't in simple-icons at all; embedding white-filled glyphs sidesteps both. Base64 is percent-encoded (`+`→`%2B`, `/`→`%2F`, `=`→`%3D`) so shields' query parser doesn't corrupt it. LinkedIn glyph is the canonical simple-icons path; Forbes "F" emblem sourced from vectorlogo.zone

---

## 2026-06-05 (patch 6)

### Changed
- Replaced the five bullet-list link sections (Connect, Thought Leadership, Social, Academic, Creative) with compact shields.io `for-the-badge` badge rows in brand colors
- Reordered Social row to lead with the highest-signal platforms (LinkedIn, Mastodon, Bluesky, X) for this audience

### Notes
- LinkedIn and Forbes badges are intentionally icon-less: simple-icons (shields' logo source) removed those logos over trademark concerns. The brand-colored text pills remain clearly recognizable; verified every other logo slug renders.

---

## 2026-06-05 (patch 5)

### Changed
- Removed all `---` horizontal rules between sections; GitHub already renders an underline beneath each `##` heading, so the explicit rules were redundant double lines
- Removed the redundant nested `bjgreenberg/` clone (an old local clone of the same remote, no unpushed work) and the now-unneeded `.gitignore` entry for it

---

## 2026-06-05 (patch 4)

### Fixed
- Hero images were distorted: `resize()` stretched every source image into the card box regardless of aspect ratio. Switched to `ImageOps.fit` (center crop + resize) so images fill the band without skew

### Documentation
- Corrected the new-tab claim: GitHub strips `target="_blank"` from README anchors, so links open in the same tab on github.com (platform limitation, no workaround). Documented in scripts/README.md

---

## 2026-06-05 (patch 3)

### Added
- `scripts/generate_cards.py` — Pillow-based generator that renders each blog/Mastodon post to a self-contained PNG card (hero image + baked title/blurb) under `assets/`, then rewrites the README sections as a borderless `<p>` of per-card image links
- `scripts/requirements.txt` (Pillow 12.2.0, defusedxml 0.7.1, both pinned)
- `scripts/README.md` — full documentation: rationale, usage, data sources, function table, troubleshooting, known limitations
- `scripts/test_generate_cards.py` — 18 pytest cases covering the pure text/HTML helpers (all passing)

### Changed
- Replaced the bordered HTML `<table>` card layout with rendered PNG cards. GitHub strips `border`/`style` from tables and applies its own cell borders, making a borderless grid impossible with live HTML; baking cards to images sidesteps this while keeping each card independently clickable
- Workflow now installs Python deps and runs `generate_cards.py` instead of inline Python; commits changed PNGs + README

### Security
- XML parsing hardened with `defusedxml` (guards against billion-laughs / XXE)
- `fetch_url` validates the URL scheme is http/https before opening
- bandit scan: 0 issues (Low/Medium/High all zero)

---

## 2026-06-05 (patch 2)

### Changed
- All static links converted from markdown syntax to `<a target="_blank" rel="noopener noreferrer">` so they open in a new tab
- Streak card image wrapped in anchor to github.com/bjgreenberg (clicking it now navigates instead of opening raw image)
- Blog and Mastodon section headers: emoji replaced with 16×16 favicon `<img>` (briangreenberg.net/favicon.ico and icons.duckduckgo.com proxy for infosec.exchange)
- "Auto-updated daily" captions: favicon appended as 12×12 inline icon after each source link
- Workflow blog template updated to emit `<a target="_blank">` so auto-generated post links also open in new tab
- Workflow Mastodon template updated the same way

---

## 2026-06-05

### Fixed
- Removed github-readme-stats card (shared Vercel instance rate-limits; same issue as prior removal 2026-05-21)
- Mastodon posts showed "[No Title] - [ID]" because Mastodon RSS has no `<title>` field; fixed by switching workflow template to `$newline_sanitized_description` capped at 140 chars

### Added
- GitHub Activity section: streak card only (streak-stats.demolab.com, tokyonight theme); top-langs and stats cards omitted — no public code and shared stats service is unreliable
- Latest from the Blog section with `BLOG-POST-LIST` markers, auto-updated daily from `briangreenberg.net/feed/`
- Latest from Mastodon section with `MASTODON-POST-LIST` markers, auto-updated daily from `infosec.exchange/@brian_greenberg.rss`
- `.github/workflows/blog-posts.yml`: daily cron (08:00 UTC) + manual dispatch; two steps — blog feed and Mastodon feed — via `gautamkrishnar/blog-post-workflow@v1`

---

## 2026-05-21

### Changed
- Replaced vague "Tech Thought Leader" bullet with specific current focus on AI-native workflows
- Rewrote bio paragraph to concretely describe work (AI systems, cybersecurity teaching, ethics writing)
- Moved Thought Leadership section above Social Networks for better signal to GitHub visitors
- Updated Twitter link and emoji to reflect X rebrand (x.com)
- Replaced bare email address with contact form link (briangreenberg.net/contact) to prevent scraping
- Removed redundant GitHub self-link from Social Networks
- Removed defunct Clubhouse link

### Fixed
- Corrected flashlight emoji (🔦) on Twitter/X → 🐦
- Corrected broken ✢️ emoji on Medium → 📝
- Corrected ski emoji (🎿) on TikTok → 🎬
- Removed duplicate 🧵 emoji (Threads and Substack both used it); Substack → 📬
- Removed unreliable third-party GitHub stats widget

---

## 2024-02-02

### Added
- Social links
- Profile image
- Initial GitHub profile README
