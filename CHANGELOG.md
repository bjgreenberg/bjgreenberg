# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## 2026-06-05

### Added
- GitHub Activity section: stats card (github-readme-stats, tokyonight theme, private contributions counted) and streak card (streak-stats.demolab.com); top-langs card omitted as public repos have no detected language
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
