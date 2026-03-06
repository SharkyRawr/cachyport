# Arch Packaging

This directory contains native Arch Linux packaging files for `cachyport`.

## Build Locally

```bash
cd build
export CACHYPORT_TAG=v0.1.0
makepkg --syncdeps --cleanbuild
```

`pkgver` is derived from `CACHYPORT_TAG` (with a leading `v` removed).
If `CACHYPORT_TAG` is not set, the PKGBUILD attempts to detect the latest git tag.
