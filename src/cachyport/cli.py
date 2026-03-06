from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_REPO = "cachyos-v3"
DEFAULT_ARCH = "x86_64_v3"
DEFAULT_MIRROR = "https://mirror.cachyos.org/repo"
FALLBACK_MIRRORS = (
    "https://cdn77.cachyos.org/repo",
    "https://cdn.cachyos.org/repo",
    "https://mirror.cachyos.org/repo",
    "https://us.cachyos.org/repo",
)
CACHE_TTL_SECONDS = 24 * 60 * 60
SUPPORTED_REPOS = ("cachyos", "cachyos-v3", "cachyos-v4", "cachyos-znver4")
SUPPORTED_ARCHES = ("x86_64", "x86_64_v3", "x86_64_v4")
REPO_ARCH_MAP = {
    "cachyos": "x86_64",
    "cachyos-v3": "x86_64_v3",
    "cachyos-v4": "x86_64_v4",
    "cachyos-znver4": "x86_64_v4",
}
SEMANTIC_PKG_FIELDS = (
    "Name",
    "Version",
    "Depends On",
    "Optional Deps",
    "Provides",
    "Conflicts With",
    "Replaces",
)
STRICT_AUDIT_PKG_FIELDS = (
    "Description",
    "URL",
    "Licenses",
    "Groups",
    "Packager",
    "Build Date",
    "Install Script",
)
PACKAGE_METADATA_FILES = {".PKGINFO", ".BUILDINFO"}
PACKAGE_INTEGRITY_FILES = {".MTREE"}

ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RESET = "\033[0m"
PKG_LINK_RE = re.compile(r'href="([^"]+\.pkg\.tar\.(?:zst|xz))"')


@dataclass
class PackageRecord:
    name: str
    version: str
    filename: str
    url: str


def supports_color(no_color: bool) -> bool:
    return not no_color and sys.stdout.isatty()


def colorize(value: str, color: str, enabled: bool) -> str:
    if not enabled:
        return value
    return f"{color}{value}{ANSI_RESET}"


def c_pkg(value: str, enabled: bool) -> str:
    return colorize(value, ANSI_YELLOW, enabled)


def c_err(value: str, enabled: bool) -> str:
    return colorize(value, ANSI_RED, enabled)


def c_ok(value: str, enabled: bool) -> str:
    return colorize(value, ANSI_GREEN, enabled)


def print_error(message: str, color_enabled: bool) -> None:
    print(c_err(f"error: {message}", color_enabled), file=sys.stderr)


def print_success(message: str, color_enabled: bool) -> None:
    print(c_ok(message, color_enabled))


def user_cache_dir() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "cachyport"


def ensure_cache_dirs() -> dict[str, Path]:
    root = user_cache_dir()
    index = root / "index"
    downloads = root / "downloads"
    backported = root / "backported"
    for path in (root, index, downloads, backported):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "index": index,
        "downloads": downloads,
        "backported": backported,
    }


def tracked_packages_file() -> Path:
    return ensure_cache_dirs()["root"] / "tracked-packages.json"


def clear_local_cache() -> None:
    cache_root = user_cache_dir()
    if cache_root.exists():
        shutil.rmtree(cache_root)


def load_tracked_packages() -> set[str]:
    state_path = tracked_packages_file()
    if not state_path.exists():
        return set()
    try:
        payload = json.loads(state_path.read_text())
    except Exception:
        return set()

    packages = payload.get("packages", [])
    if not isinstance(packages, list):
        return set()
    return {item for item in packages if isinstance(item, str) and item}


def save_tracked_packages(packages: set[str]) -> None:
    state_path = tracked_packages_file()
    state_path.write_text(json.dumps({"packages": sorted(packages)}, indent=2))


def remember_tracked_packages(package_names: list[str]) -> None:
    tracked = load_tracked_packages()
    tracked.update(package_names)
    save_tracked_packages(tracked)


def validate_repo_arch(repo: str, arch: str) -> None:
    expected = REPO_ARCH_MAP.get(repo)
    if expected and arch != expected:
        raise RuntimeError(
            f"repo {repo} requires --arch {expected}, but got --arch {arch}"
        )


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def candidate_mirrors(primary: str) -> list[str]:
    mirrors = [primary, *FALLBACK_MIRRORS]
    normalized: list[str] = []
    seen: set[str] = set()
    for mirror in mirrors:
        value = mirror.rstrip("/")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def run_command(
    command: list[str], capture: bool = False
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, check=False, capture_output=capture, text=True)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        extra = f": {stderr}" if stderr else ""
        raise RuntimeError(
            f"command failed ({proc.returncode}): {shell_join(command)}{extra}"
        )
    return proc


def fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "cachyport/0.1 (+https://github.com)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError, URLError:
        curl = subprocess.run(
            ["curl", "-fsSL", "-A", "cachyport/0.1", url],
            capture_output=True,
            text=True,
            check=False,
        )
        if curl.returncode != 0:
            raise RuntimeError(f"failed to fetch {url}: {curl.stderr.strip()}")
        return curl.stdout


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "cachyport/0.1 (+https://github.com)"})
    try:
        with urlopen(req, timeout=60) as response, dest.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        return
    except HTTPError, URLError:
        pass

    curl = subprocess.run(["curl", "-fL", "-o", str(dest), url], check=False)
    if curl.returncode != 0:
        raise RuntimeError(f"failed to download {url}")


def download_file_with_failover(urls: list[str], dest: Path) -> str:
    errors: list[str] = []
    for url in urls:
        dest.unlink(missing_ok=True)
        try:
            download_file(url, dest)
            return url
        except RuntimeError as exc:
            dest.unlink(missing_ok=True)
            errors.append(str(exc))
    raise RuntimeError("all mirrors failed: " + " | ".join(errors))


def parse_directory_packages(html: str, base_url: str) -> list[PackageRecord]:
    seen: set[str] = set()
    records: list[PackageRecord] = []

    for match in PKG_LINK_RE.findall(html):
        filename = match.split("/")[-1]
        if filename in seen:
            continue
        seen.add(filename)

        stem = filename.split(".pkg.tar.")[0]
        parts = stem.rsplit("-", 3)
        if len(parts) != 4:
            continue
        name, pkgver, pkgrel, _arch = parts
        records.append(
            PackageRecord(
                name=name,
                version=f"{pkgver}-{pkgrel}",
                filename=filename,
                url=urljoin(
                    base_url if base_url.endswith("/") else f"{base_url}/", filename
                ),
            )
        )

    return records


def load_index(
    repo: str, arch: str, mirror: str, refresh: bool, cache_ttl: int
) -> list[PackageRecord]:
    validate_repo_arch(repo, arch)
    dirs = ensure_cache_dirs()
    index_file = dirs["index"] / f"{repo}-{arch}.json"

    if index_file.exists() and not refresh:
        try:
            payload = json.loads(index_file.read_text())
            age = int(payload.get("fetched_at", 0))
            if (int(time.time()) - age) < cache_ttl:
                return [PackageRecord(**item) for item in payload.get("packages", [])]
        except Exception:
            pass

    packages: list[PackageRecord] = []
    errors: list[str] = []
    for mirror_root in candidate_mirrors(mirror):
        repo_url = f"{mirror_root}/{arch}/{repo}/"
        try:
            html = fetch_text(repo_url)
            packages = parse_directory_packages(html, repo_url)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue

        if packages:
            break
        errors.append(f"no packages found in repository index: {repo_url}")

    if not packages:
        raise RuntimeError(
            "unable to load package index from mirrors: " + " | ".join(errors)
        )

    payload = {
        "repo": repo,
        "arch": arch,
        "fetched_at": int(time.time()),
        "packages": [record.__dict__ for record in packages],
    }
    index_file.write_text(json.dumps(payload, indent=2))
    return packages


def normalize_variant(name: str) -> str:
    if name.startswith("linux-cachyos"):
        return name
    aliases = {
        "default": "linux-cachyos",
        "stock": "linux-cachyos",
        "linux": "linux-cachyos",
        "mainline": "linux-cachyos",
    }
    if name in aliases:
        return aliases[name]
    return f"linux-cachyos-{name}"


def kernel_packages(records: list[PackageRecord]) -> list[PackageRecord]:
    return [item for item in records if item.name.startswith("linux-cachyos")]


def package_map(records: list[PackageRecord]) -> dict[str, PackageRecord]:
    return {item.name: item for item in records}


def parse_pacman_q_output(output: str) -> dict[str, str]:
    installed: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        installed[parts[0]] = parts[1]
    return installed


def get_installed_versions_for_names(package_names: set[str]) -> dict[str, str]:
    if not package_names:
        return {}
    details = run_command(["pacman", "-Q", *sorted(package_names)], capture=True)
    return parse_pacman_q_output(details.stdout)


def installed_package_name_set() -> set[str]:
    proc = run_command(["pacman", "-Qq"], capture=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def get_installed_cachyos_package_versions() -> dict[str, str]:
    names = {
        name
        for name in installed_package_name_set()
        if name.startswith("linux-cachyos")
    }
    return get_installed_versions_for_names(names)


def get_installed_managed_package_versions(
    available: dict[str, PackageRecord],
) -> dict[str, str]:
    installed_names = installed_package_name_set()
    tracked = load_tracked_packages()
    kernel_names = {
        name for name in installed_names if name.startswith("linux-cachyos")
    }
    candidates = (kernel_names | tracked) & installed_names & set(available)
    return get_installed_versions_for_names(candidates)


def parse_pacman_qip_output(output: str) -> dict[str, str]:
    info: dict[str, str] = {}
    current_key: str | None = None
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            current_key = key.strip()
            info[current_key] = value.strip()
            continue
        if current_key is None:
            continue

        value = line.strip()
        if not value:
            continue
        existing = info.get(current_key, "")
        info[current_key] = f"{existing}\n{value}" if existing else value
    return info


def package_info(path: Path) -> dict[str, str]:
    proc = run_command(["pacman", "-Qip", str(path)], capture=True)
    return parse_pacman_qip_output(proc.stdout)


def normalize_field_value(value: str) -> str:
    parts = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
    return "\n".join(parts)


def validate_repacked_metadata(
    original_pkg: Path, repacked_pkg: Path, strict_audit: bool
) -> None:
    if original_pkg == repacked_pkg:
        return

    original = package_info(original_pkg)
    repacked = package_info(repacked_pkg)

    source_arch = original.get("Architecture", "")
    target_arch = repacked.get("Architecture", "")
    if source_arch not in {"x86_64_v3", "x86_64_v4"}:
        raise RuntimeError(
            f"unexpected source package architecture in {original_pkg.name}: {source_arch}"
        )
    if target_arch != "x86_64":
        raise RuntimeError(
            f"repacked package architecture is {target_arch}, expected x86_64"
        )

    fields = [*SEMANTIC_PKG_FIELDS]
    if strict_audit:
        fields.extend(STRICT_AUDIT_PKG_FIELDS)

    differences: list[str] = []
    for field in fields:
        original_value = normalize_field_value(original.get(field, ""))
        repacked_value = normalize_field_value(repacked.get(field, ""))
        if original_value != repacked_value:
            differences.append(field)

    if differences:
        fields = ", ".join(differences)
        raise RuntimeError(
            f"metadata mismatch after repack for {repacked_pkg.name}: {fields}"
        )


def compare_versions(new_version: str, old_version: str) -> int:
    proc = run_command(["vercmp", new_version, old_version], capture=True)
    return int(proc.stdout.strip())


def rewrite_metadata_contents(member_name: str, data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data

    if member_name == ".PKGINFO":
        rewritten = re.sub(
            r"^arch = x86_64_v[34]$", "arch = x86_64", text, flags=re.MULTILINE
        )
        return rewritten.encode("utf-8")

    if member_name == ".BUILDINFO":
        rewritten = re.sub(
            r"^arch = x86_64_v[34]$", "arch = x86_64", text, flags=re.MULTILINE
        )
        rewritten = re.sub(
            r"^buildenv = arch=x86_64_v[34]$",
            "buildenv = arch=x86_64",
            rewritten,
            flags=re.MULTILINE,
        )
        return rewritten.encode("utf-8")

    return data


def repack_with_arch_port(downloaded_pkg: Path, output_dir: Path) -> Path:
    stem, suffix = downloaded_pkg.name.split(".pkg.tar.", 1)
    name, pkgver, pkgrel, arch = stem.rsplit("-", 3)
    if arch not in {"x86_64_v3", "x86_64_v4"}:
        return downloaded_pkg

    out_name = f"{name}-{pkgver}-{pkgrel}-x86_64.pkg.tar.{suffix}"
    out_path = output_dir / out_name
    if out_path.exists():
        return out_path

    if suffix == "zst":
        out_mode = "w:zst"
    elif suffix == "xz":
        out_mode = "w:xz"
    else:
        raise RuntimeError(f"unsupported package suffix: {suffix}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with (
        tarfile.open(downloaded_pkg, mode="r:*") as source,
        tarfile.open(out_path, mode=out_mode) as target,
    ):
        for member in source:
            basename = Path(member.name).name
            if basename in PACKAGE_INTEGRITY_FILES:
                continue

            if member.isfile():
                source_file = source.extractfile(member)
                if source_file is None:
                    raise RuntimeError(f"failed to read member {member.name}")
                with source_file:
                    if basename in PACKAGE_METADATA_FILES:
                        raw = source_file.read()
                        rewritten = rewrite_metadata_contents(basename, raw)
                        new_member = copy.copy(member)
                        new_member.size = len(rewritten)
                        target.addfile(new_member, io.BytesIO(rewritten))
                    else:
                        target.addfile(member, source_file)
            else:
                target.addfile(member)

    return out_path


def repack_with_arch_port_force(
    downloaded_pkg: Path, output_dir: Path, force: bool
) -> Path:
    if not force:
        return repack_with_arch_port(downloaded_pkg, output_dir)

    stem, suffix = downloaded_pkg.name.split(".pkg.tar.", 1)
    name, pkgver, pkgrel, arch = stem.rsplit("-", 3)
    if arch not in {"x86_64_v3", "x86_64_v4"}:
        return downloaded_pkg

    out_name = f"{name}-{pkgver}-{pkgrel}-x86_64.pkg.tar.{suffix}"
    out_path = output_dir / out_name
    out_path.unlink(missing_ok=True)
    return repack_with_arch_port(downloaded_pkg, output_dir)


def resolve_requested_names(
    requested: list[str], available: dict[str, PackageRecord]
) -> list[str]:
    resolved: list[str] = []
    for name in requested:
        if name in available:
            resolved.append(name)
            continue
        normalized = normalize_variant(name)
        if normalized in available:
            resolved.append(normalized)
            continue
        raise RuntimeError(f"requested package not found in repo: {name}")
    return sorted(set(resolved))


def prepare_packages_for_install(
    package_names: list[str],
    available: dict[str, PackageRecord],
    repo: str,
    arch: str,
    mirror: str,
    refresh: bool,
    force: bool,
    strict_audit: bool,
    dry_run: bool,
    color_enabled: bool,
) -> list[Path]:
    dirs = ensure_cache_dirs()
    local_paths: list[Path] = []

    for package_name in package_names:
        record = available[package_name]
        download_path = dirs["downloads"] / record.filename

        if dry_run:
            print(
                f"Would prepare {c_pkg(record.filename, color_enabled)} "
                f"from {mirror}/{arch}/{repo}/"
            )
            continue

        if force:
            download_path.unlink(missing_ok=True)

        if force or refresh or not download_path.exists():
            urls = [
                f"{mirror_root}/{arch}/{repo}/{record.filename}"
                for mirror_root in candidate_mirrors(mirror)
            ]
            print(f"Downloading {c_pkg(record.filename, color_enabled)}")
            used = download_file_with_failover(urls, download_path)
            if used != urls[0]:
                print_success(f"Mirror failover succeeded via {used}", color_enabled)

        repacked = repack_with_arch_port_force(download_path, dirs["backported"], force)
        validate_repacked_metadata(download_path, repacked, strict_audit)
        local_paths.append(repacked)
        print_success(f"Ready {repacked.name}", color_enabled)

    return local_paths


def install_local_packages(
    paths: list[Path],
    installed_names: list[str],
    assume_yes: bool,
    color_enabled: bool,
) -> None:
    cmd = ["sudo", "pacman", "-U"]
    if assume_yes:
        cmd.append("--noconfirm")
    cmd.extend(str(path) for path in paths)
    print(f"Running: {shell_join(cmd)}")
    run_command(cmd)
    ensure_boot_kver_files(installed_names, color_enabled)
    remember_tracked_packages(installed_names)
    print_success("Install complete", color_enabled)


def package_file_list(package_name: str) -> list[Path]:
    proc = run_command(["pacman", "-Qlq", package_name], capture=True)
    return [Path(line.strip()) for line in proc.stdout.splitlines() if line.strip()]


def module_pkgbase_paths(package_name: str) -> list[Path]:
    result: list[Path] = []
    for path in package_file_list(package_name):
        parts = path.parts
        if len(parts) < 6:
            continue
        if parts[:4] != ("/", "usr", "lib", "modules"):
            continue
        if path.name != "pkgbase":
            continue
        result.append(path)
    return result


def ensure_boot_kver_files(package_names: list[str], color_enabled: bool) -> None:
    updates: list[str] = []
    for package_name in package_names:
        for pkgbase_path in module_pkgbase_paths(package_name):
            try:
                pkgbase = pkgbase_path.read_text().strip()
            except OSError:
                continue
            if not pkgbase:
                continue

            kernel_release = pkgbase_path.parent.name
            boot_kver = Path("/boot") / f"{pkgbase}.kver"
            desired = f"{kernel_release}\n"

            current = None
            try:
                current = boot_kver.read_text()
            except OSError:
                pass

            if current == desired:
                continue

            cmd = [
                "sudo",
                "sh",
                "-c",
                f"printf '%s' {shlex.quote(desired)} > {shlex.quote(str(boot_kver))}",
            ]
            run_command(cmd)
            updates.append(boot_kver.name)

    if updates:
        joined = ", ".join(sorted(set(updates)))
        print_success(f"Created/updated kver files: {joined}", color_enabled)


def validate_action_flags(args: argparse.Namespace) -> None:
    if not args.list and args.installed:
        raise RuntimeError("--installed can only be used with --list")

    install_update = bool(args.install) or args.update
    if not install_update and args.assume_yes:
        raise RuntimeError("--assume-yes can only be used with --install or --update")
    if not install_update and args.download_only:
        raise RuntimeError(
            "--download-only can only be used with --install or --update"
        )
    if not install_update and args.force:
        raise RuntimeError("--force can only be used with --install or --update")
    if not install_update and args.dry_run:
        raise RuntimeError("--dry-run can only be used with --install or --update")
    if not install_update and args.strict_audit:
        raise RuntimeError("--strict-audit can only be used with --install or --update")

    if args.clean and args.refresh:
        raise RuntimeError("--refresh cannot be used with --clean")


def handle_list(args: argparse.Namespace, color_enabled: bool) -> int:
    records = kernel_packages(
        load_index(args.repo, args.arch, args.mirror, args.refresh, args.cache_ttl)
    )
    installed = get_installed_cachyos_package_versions()

    shown = sorted(records, key=lambda item: item.name)
    if args.installed:
        shown = [item for item in shown if item.name in installed]

    for item in shown:
        marker = ""
        if item.name in installed:
            marker = f" {c_ok('[installed]', color_enabled)}"
        print(f"{c_pkg(item.name, color_enabled):42} {item.version}{marker}")

    print_success(f"Displayed {len(shown)} packages", color_enabled)
    return 0


def handle_install(args: argparse.Namespace, color_enabled: bool) -> int:
    records = load_index(
        args.repo, args.arch, args.mirror, args.refresh, args.cache_ttl
    )
    available = package_map(records)

    resolved = resolve_requested_names(args.install, available)
    paths = prepare_packages_for_install(
        resolved,
        available,
        args.repo,
        args.arch,
        args.mirror,
        args.refresh,
        args.force,
        args.strict_audit,
        args.dry_run,
        color_enabled,
    )

    if args.dry_run:
        print_success(
            "Dry run complete (no downloads or installs performed)", color_enabled
        )
        return 0

    if args.download_only:
        print_success("Download/backport complete (skipped install)", color_enabled)
        return 0

    install_local_packages(paths, resolved, args.assume_yes, color_enabled)
    return 0


def handle_update(args: argparse.Namespace, color_enabled: bool) -> int:
    records = kernel_packages(
        load_index(args.repo, args.arch, args.mirror, args.refresh, args.cache_ttl)
    )
    available = package_map(records)
    installed = get_installed_managed_package_versions(available)

    update_targets: list[str] = []
    skipped_not_newer = 0
    for name, installed_version in installed.items():
        if name not in available:
            continue
        repo_version = available[name].version
        cmp_result = compare_versions(repo_version, installed_version)
        if cmp_result > 0:
            update_targets.append(name)
        else:
            skipped_not_newer += 1

    if not update_targets:
        print_success(
            "No updates available for installed CachyOS kernel packages", color_enabled
        )
        return 0

    print(f"Updating {len(update_targets)} package(s):")
    for name in sorted(update_targets):
        print(
            f"- {c_pkg(name, color_enabled)} "
            f"({installed[name]} -> {available[name].version})"
        )

    if skipped_not_newer:
        print_success(
            f"Skipped {skipped_not_newer} package(s) that are not newer upstream",
            color_enabled,
        )

    paths = prepare_packages_for_install(
        sorted(update_targets),
        available,
        args.repo,
        args.arch,
        args.mirror,
        args.refresh,
        args.force,
        args.strict_audit,
        args.dry_run,
        color_enabled,
    )

    if args.dry_run:
        print_success(
            "Dry run complete (no downloads or installs performed)", color_enabled
        )
        return 0
    if args.download_only:
        print_success(
            "Update packages downloaded/backported (skipped install)", color_enabled
        )
        return 0

    install_local_packages(
        paths,
        sorted(update_targets),
        args.assume_yes,
        color_enabled,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cachyport",
        description=(
            "Download CachyOS kernel packages, port architecture metadata to Arch-compatible "
            "x86_64, and install updates locally."
        ),
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--list", action="store_true", help="List available CachyOS kernel packages"
    )
    action.add_argument(
        "--install", nargs="+", metavar="PKG", help="Port and install package(s)"
    )
    action.add_argument(
        "--update", action="store_true", help="Update installed CachyOS kernel packages"
    )
    action.add_argument(
        "--clean", action="store_true", help="Remove local cachyport cache data"
    )

    parser.add_argument(
        "--installed",
        action="store_true",
        help="With --list, show only installed packages",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        choices=SUPPORTED_REPOS,
        help=(
            "CachyOS repo "
            "(choices: cachyos, cachyos-v3, cachyos-v4, cachyos-znver4; "
            "default: cachyos-v3)"
        ),
    )
    parser.add_argument(
        "--arch",
        default=DEFAULT_ARCH,
        choices=SUPPORTED_ARCHES,
        help=(
            "CachyOS architecture "
            "(choices: x86_64, x86_64_v3, x86_64_v4; default: x86_64_v3)"
        ),
    )
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help="Mirror root URL")
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=CACHE_TTL_SECONDS,
        help="Index cache TTL in seconds",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh cached index and package downloads",
    )
    parser.add_argument(
        "--assume-yes", action="store_true", help="Pass --noconfirm to pacman"
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Download/repack but skip pacman -U",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --install/--update, bypass cached downloads and backported packages",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --install/--update, show planned actions without changes",
    )
    parser.add_argument(
        "--strict-audit",
        action="store_true",
        help="With --install/--update, compare additional package metadata fields",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable colored output"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    color_enabled = supports_color(args.no_color)

    try:
        validate_action_flags(args)
        if args.list:
            return handle_list(args, color_enabled)
        if args.install:
            return handle_install(args, color_enabled)
        if args.update:
            return handle_update(args, color_enabled)
        if args.clean:
            clear_local_cache()
            print_success("Removed local cache data", color_enabled)
            return 0
        raise RuntimeError("no action selected")
    except KeyboardInterrupt:
        print_error("interrupted by user", color_enabled)
        return 130
    except Exception as exc:
        print_error(str(exc), color_enabled)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
