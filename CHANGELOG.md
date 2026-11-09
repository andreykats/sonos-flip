# Changelog

What changed in each release. Each release's section becomes its GitHub release notes, and the
whole changelog is shown on the Docker Hub page.

New changes go under "Unreleased" until the next release; releasing renames that section to the version.

## Unreleased

## 0.2.0 - 2026-11-09

### Added

- The pair keeps its EQ across a switch to theater and back. Sonos loses the speakers' bass, treble, loudness and balance when they become surrounds; the service now saves them before splitting the pair and sets them back after re-pairing for desk. If that fails, the switch still completes and the response says why (`"eq": "save failed: …"` or `"restore failed: …"`). Trueplay tuning can't be set back this way.

## 0.1.0 - 2026-11-04

First public release.

### Added

- Switches two Sonos speakers between the soundbar's rear surrounds (theater) and a standalone stereo pair (desk).
- Switches to theater when TV audio starts on the soundbar, and back to desk when it stops, after an optional wait (`desk_after_tv_min`).
- HTTP API: `POST /desk`, `POST /theater`, `GET /status`, `GET /soundbar`, `GET /health`.
- Speakers given by room name or IP address; paired or bonded speakers are recognised by their place in the pair or on the soundbar, and remembered.
- Keeps the pair's room name (its AirPlay name) after every switch.
- Optional line-in: after every switch to desk, plays one speaker's line-in at a set volume, and sets the previous volume back on the next switch to theater.
- One `config.toml`, with clear errors for wrong settings; a commented template is written on first start.
- Docker image for amd64 and arm64.
