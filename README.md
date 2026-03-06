# cachyport

`cachyport` is a Python CLI to fetch CachyOS kernel packages, port their package
metadata for Arch compatibility, and install them locally.

It focuses on CachyOS kernel packages from the CachyOS binary repos and keeps
index fetches cached for one day to keep repeated commands fast.

## What it does

- Downloads upstream CachyOS `.pkg.tar.*` kernel packages.
- Ports architecture metadata (`x86_64_v3` / `x86_64_v4` -> `x86_64`) so Arch can install them.
- Preserves package metadata semantics (depends/provides/conflicts/etc.) from upstream.
- Installs packages via `pacman -U`.
- Highlights package names in yellow, errors in red, and success in green.

Uninstall remains the normal Arch workflow: `pacman -R ...`

## Requirements

- Linux with `pacman`
- Python 3.14+
- `uv`
- `curl` (used as fallback for some mirror fetch/download cases)

## Setup

```bash
uv sync
```

## Usage

Run through `uv`:

```bash
uv run cachyport --list
uv run cachyport --install linux-cachyos
uv run cachyport --update
```

### Commands

- `--list` list available CachyOS kernel packages.
- `--list --installed` list only installed CachyOS kernel packages.
- `--install <pkg...>` port and install one or more packages.
- `--update` check installed CachyOS kernel packages and only install when upstream is newer.

### Useful flags

- `--refresh` force refresh repo index cache and redownload packages.
- `--download-only` perform download + porting without running `pacman -U`.
- `--assume-yes` pass `--noconfirm` to `pacman`.
- `--no-color` disable ANSI colors.
- `--repo`, `--arch`, `--mirror` override source settings.

## Cache behavior

- Index cache location: `~/.cache/cachyport/index/`
- Download cache location: `~/.cache/cachyport/downloads/`
- Repacked package location: `~/.cache/cachyport/backported/`
- Default index cache TTL: 24 hours

Use `--refresh` to rebuild cache immediately.

## Notes

- Installation requires root because `pacman -U` is run with `sudo`.
- Package updates are determined by comparing installed versions with repo versions via `vercmp`.
