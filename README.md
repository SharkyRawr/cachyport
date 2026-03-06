# cachyport

`cachyport` is a command-line tool for Arch Linux users who want to install
CachyOS kernel packages by porting package metadata to an Arch-compatible form.

Repository: https://github.com/SharkyRawr/cachyport

License: CC-BY-NC-SA 4.0

## Scope and Safety

`cachyport` is intentionally focused on the CachyOS kernel family
(`linux-cachyos*`). It does **not** support general system package porting,
because cross-distribution system packages can introduce ABI and dependency
breakage.

The only non-kernel exception is `cachyos-keyring`, provided via
`--bootstrap-keyring` so signature verification can be enabled.

## Features

- Lists available CachyOS kernel packages.
- Downloads upstream CachyOS kernel binaries.
- Rewrites architecture metadata (`x86_64_v3` / `x86_64_v4` to `x86_64`).
- Verifies semantic metadata parity after repacking.
- Verifies detached signatures by default (`pacman-key --verify`).
- Supports mirror failover with local reliability/latency scoring.
- Caches repository indexes for 24 hours.
- Installs via `pacman -U` and repairs missing `/boot/*.kver` files.

## Requirements

- Arch Linux (or compatible system with `pacman`)
- Python 3.14+
- `uv`
- `curl`

## Development Setup

```bash
uv sync
```

## Usage

```bash
uv run cachyport --list
uv run cachyport --install linux-cachyos
uv run cachyport --update
```

### Core Commands

- `--list`: list available CachyOS kernel packages.
- `--list --installed`: only show installed kernel packages.
- `--install <pkg...>`: install kernel-family packages (`linux-cachyos*`).
- `--update`: update installed kernel-family packages when upstream is newer.
- `--bootstrap-keyring`: install `cachyos-keyring` to enable trust setup.
- `--doctor`: run preflight checks (tools, repo access, signature path).
- `--clean`: clear local cache (`~/.cache/cachyport`).

### Common Flags

- `--refresh`: refresh index/download cache.
- `--force`: bypass cached downloads/backports.
- `--download-only`: skip `pacman -U`.
- `--dry-run`: print planned actions only.
- `--strict-audit`: compare additional metadata fields after repack.
- `--skip-signature-check`: skip package signature verification for install/update.
- `--allow-unsigned-keyring`: allow unsigned keyring bootstrap only.
- `--assume-yes`: pass `--noconfirm` to pacman.

## Cache Locations

- Index cache: `~/.cache/cachyport/index/`
- Download cache: `~/.cache/cachyport/downloads/`
- Backported packages: `~/.cache/cachyport/backported/`
- Tracked package state: `~/.cache/cachyport/tracked-packages.json`
- Mirror stats: `~/.cache/cachyport/mirror-stats.json`

## Packaging for Arch Linux

Native Arch packaging files are provided in `build/`.

```bash
cd build
export CACHYPORT_TAG=v0.1.0
makepkg --syncdeps --cleanbuild
```

`PKGBUILD` derives `pkgver` from the current git tag (`CACHYPORT_TAG`),
removing a leading `v` when present.

## Release Automation

On tag push, GitHub Actions will:

1. Build the native Arch package from `build/PKGBUILD`.
2. Generate SHA256 and SHA512 checksum files.
3. Create a GitHub Release and upload:
   - the built package
   - `SHA256SUMS`
   - `SHA512SUMS`
4. Publish checksums in the release notes for verification.

Workflow file: `.github/workflows/release.yml`

## Troubleshooting

- Signature failures usually mean the CachyOS signing keys are not installed or
  trusted in your pacman keyring.
- Start with:

  ```bash
  uv run cachyport --bootstrap-keyring
  uv run cachyport --doctor
  ```

- If you accept the trust tradeoff for first bootstrap, use
  `--allow-unsigned-keyring` with `--bootstrap-keyring`.
