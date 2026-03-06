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
- `--force` with `--install`/`--update` bypasses cached downloads and backported packages.
- `--clean` removes local `cachyport` cache data (`~/.cache/cachyport`).
- `--assume-yes` pass `--noconfirm` to `pacman`.
- `--no-color` disable ANSI colors.
- `--repo`, `--arch`, `--mirror` override source settings.
  - repos: `cachyos`, `cachyos-v3`, `cachyos-v4`, `cachyos-znver4`
  - arches: `x86_64`, `x86_64_v3`, `x86_64_v4`

## Cache behavior

- Index cache location: `~/.cache/cachyport/index/`
- Download cache location: `~/.cache/cachyport/downloads/`
- Repacked package location: `~/.cache/cachyport/backported/`
- Default index cache TTL: 24 hours

Use `--refresh` to rebuild cache immediately.

## Notes

- Installation requires root because `pacman -U` is run with `sudo`.
- Package updates are determined by comparing installed versions with repo versions via `vercmp`.
- `--update` checks installed `linux-cachyos*` packages and packages previously installed via `cachyport --install`.
- Repo/arch combinations are validated (`cachyos->x86_64`, `cachyos-v3->x86_64_v3`, `cachyos-v4->x86_64_v4`, `cachyos-znver4->x86_64_v4`).
- Repacked packages are verified with `pacman -Qip` to ensure semantic fields (depends/provides/conflicts/replaces/optional deps) are unchanged.
- Repacking removes upstream `.MTREE` from the local ported package to avoid stale integrity metadata after arch rewrite.
- After install/update, `cachyport` ensures `/boot/<pkgbase>.kver` exists for installed kernel packages.
