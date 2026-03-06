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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
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


def mirror_stats_file() -> Path:
    return ensure_cache_dirs()["root"] / "mirror-stats.json"


def clear_local_cache() -> None:
    cache_root = user_cache_dir()
    if cache_root.exists():
        shutil.rmtree(cache_root)


def load_mirror_stats() -> dict[str, dict[str, float]]:
    path = mirror_stats_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, dict[str, float]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        normalized[key] = {
            "ok": float(value.get("ok", 0.0)),
            "fail": float(value.get("fail", 0.0)),
            "latency_ms": float(value.get("latency_ms", 0.0)),
        }
    return normalized


def save_mirror_stats(stats: dict[str, dict[str, float]]) -> None:
    mirror_stats_file().write_text(json.dumps(stats, indent=2, sort_keys=True))


def mirror_root_from_url(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "repo" in parts:
        repo_index = parts.index("repo")
        root_path = "/" + "/".join(parts[: repo_index + 1])
    elif parts:
        root_path = "/" + parts[0]
    else:
        root_path = ""
    return f"{parsed.scheme}://{parsed.netloc}{root_path}"


def record_mirror_result(url: str, success: bool, latency_ms: float) -> None:
    root = mirror_root_from_url(url)
    stats = load_mirror_stats()
    entry = stats.get(root, {"ok": 0.0, "fail": 0.0, "latency_ms": 0.0})
    if success:
        entry["ok"] = entry.get("ok", 0.0) + 1
        current = entry.get("latency_ms", 0.0)
        entry["latency_ms"] = (
            latency_ms if current <= 0 else ((current * 0.7) + (latency_ms * 0.3))
        )
    else:
        entry["fail"] = entry.get("fail", 0.0) + 1
    stats[root] = entry
    save_mirror_stats(stats)


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

    primary_normalized = primary.rstrip("/")
    others = [value for value in normalized if value != primary_normalized]
    stats = load_mirror_stats()

    def mirror_score(mirror: str) -> tuple[float, float, str]:
        entry = stats.get(mirror, {})
        ok = float(entry.get("ok", 0.0))
        fail = float(entry.get("fail", 0.0))
        latency = float(entry.get("latency_ms", 0.0)) or 999999.0
        reliability = ok - (fail * 2.0)
        return (-reliability, latency, mirror)

    others_sorted = sorted(others, key=mirror_score)
    if primary_normalized in normalized:
        return [primary_normalized, *others_sorted]
    return others_sorted


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
        started = time.perf_counter()
        try:
            download_file(url, dest)
            record_mirror_result(url, True, (time.perf_counter() - started) * 1000.0)
            return url
        except RuntimeError as exc:
            dest.unlink(missing_ok=True)
            record_mirror_result(url, False, (time.perf_counter() - started) * 1000.0)
            errors.append(str(exc))
    raise RuntimeError("all mirrors failed: " + " | ".join(errors))


def verify_package_signature(package_path: Path, signature_path: Path) -> None:
    try:
        run_command(
            ["pacman-key", "--verify", str(signature_path), str(package_path)],
            capture=True,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "signature verification failed for "
            f"{package_path.name}. Install/trust CachyOS keyring or use "
            "--skip-signature-check to bypass. "
            f"Details: {exc}"
        ) from exc


def parse_repo_db_desc(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    values: list[str] = []

    def flush() -> None:
        nonlocal current, values
        if current is None:
            return
        fields[current] = "\n".join(values)
        current = None
        values = []

    for raw in content.splitlines():
        line = raw.strip("\n")
        if line.startswith("%") and line.endswith("%") and len(line) > 2:
            flush()
            current = line.strip("%")
            values = []
            continue
        values.append(line)
    flush()
    return fields


def parse_repo_db(db_path: Path, base_url: str) -> list[PackageRecord]:
    records: list[PackageRecord] = []
    with tarfile.open(db_path, mode="r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            fileobj = tf.extractfile(member)
            if fileobj is None:
                continue
            text = fileobj.read().decode("utf-8", errors="replace")
            fields = parse_repo_db_desc(text)
            name = fields.get("NAME", "").strip()
            version = fields.get("VERSION", "").strip()
            filename = fields.get("FILENAME", "").strip()
            if not name or not version or not filename:
                continue
            records.append(
                PackageRecord(
                    name=name,
                    version=version,
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
        db_url = f"{repo_url}{repo}.db"

        with tempfile.TemporaryDirectory(prefix="cachyport-db-") as tmp:
            db_path = Path(tmp) / f"{repo}.db"
            started = time.perf_counter()
            try:
                download_file(db_url, db_path)
            except RuntimeError as exc:
                record_mirror_result(
                    db_url, False, (time.perf_counter() - started) * 1000.0
                )
                errors.append(str(exc))
                continue
            record_mirror_result(db_url, True, (time.perf_counter() - started) * 1000.0)

            try:
                packages = parse_repo_db(db_path, repo_url)
            except (tarfile.TarError, OSError) as exc:
                errors.append(f"failed to parse repo database {db_url}: {exc}")
                continue

        if packages:
            break
        errors.append(f"no packages found in repo database: {db_url}")

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


def verify_repo_index_signature(repo: str, arch: str, mirror: str) -> str:
    with tempfile.TemporaryDirectory(prefix="cachyport-db-verify-") as tmp:
        db_path = Path(tmp) / f"{repo}.db"
        sig_path = Path(tmp) / f"{repo}.db.sig"
        db_urls = [
            f"{mirror_root}/{arch}/{repo}/{repo}.db"
            for mirror_root in candidate_mirrors(mirror)
        ]
        used = download_file_with_failover(db_urls, db_path)
        sig_urls = [f"{url}.sig" for url in db_urls]
        download_file_with_failover(sig_urls, sig_path)
        verify_package_signature(db_path, sig_path)
        return used


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

    unique = sorted(set(resolved))
    unsupported = [item for item in unique if not item.startswith("linux-cachyos")]
    if unsupported:
        raise RuntimeError(
            "unsupported package selection: "
            + ", ".join(unsupported)
            + ". cachyport only supports CachyOS kernel packages (linux-cachyos*)."
        )
    return unique


def prepare_packages_for_install(
    package_names: list[str],
    available: dict[str, PackageRecord],
    repo: str,
    arch: str,
    mirror: str,
    refresh: bool,
    force: bool,
    strict_audit: bool,
    verify_signature: bool,
    dry_run: bool,
    color_enabled: bool,
) -> list[Path]:
    dirs = ensure_cache_dirs()
    local_paths: list[Path] = []

    for package_name in package_names:
        record = available[package_name]
        download_path = dirs["downloads"] / record.filename
        urls = [
            f"{mirror_root}/{arch}/{repo}/{record.filename}"
            for mirror_root in candidate_mirrors(mirror)
        ]

        if dry_run:
            print(
                f"Would prepare {c_pkg(record.filename, color_enabled)} "
                f"from {mirror}/{arch}/{repo}/"
            )
            continue

        if force:
            download_path.unlink(missing_ok=True)

        if force or refresh or not download_path.exists():
            print(f"Downloading {c_pkg(record.filename, color_enabled)}")
            used = download_file_with_failover(urls, download_path)
            if used != urls[0]:
                print_success(f"Mirror failover succeeded via {used}", color_enabled)

        if verify_signature:
            sig_path = download_path.with_name(f"{download_path.name}.sig")
            if force:
                sig_path.unlink(missing_ok=True)
            if force or refresh or not sig_path.exists():
                sig_urls = [f"{url}.sig" for url in urls]
                print(
                    f"Downloading signature for {c_pkg(record.filename, color_enabled)}"
                )
                used_sig = download_file_with_failover(sig_urls, sig_path)
                if used_sig != sig_urls[0]:
                    print_success(
                        f"Signature mirror failover succeeded via {used_sig}",
                        color_enabled,
                    )
            verify_package_signature(download_path, sig_path)

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
    if not install_update and args.skip_signature_check:
        raise RuntimeError(
            "--skip-signature-check can only be used with --install or --update"
        )

    if args.clean and args.refresh:
        raise RuntimeError("--refresh cannot be used with --clean")


def handle_doctor(args: argparse.Namespace, color_enabled: bool) -> int:
    checks = ["pacman", "pacman-key", "vercmp", "curl", "sudo"]
    missing = [binary for binary in checks if shutil.which(binary) is None]
    if missing:
        raise RuntimeError("missing required tools: " + ", ".join(missing))

    validate_repo_arch(args.repo, args.arch)
    load_index(args.repo, args.arch, args.mirror, True, args.cache_ttl)
    used_mirror = verify_repo_index_signature(args.repo, args.arch, args.mirror)
    print_success(f"Verified repo index signature via {used_mirror}", color_enabled)

    print_success("Doctor checks passed", color_enabled)
    return 0


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
        not args.skip_signature_check,
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
            "No updates available for installed CachyOS kernel packages",
            color_enabled,
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
        not args.skip_signature_check,
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
            "Download CachyOS kernel packages (linux-cachyos*), port architecture "
            "metadata to Arch-compatible x86_64, and install updates locally."
        ),
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--list", action="store_true", help="List available CachyOS kernel packages"
    )
    action.add_argument(
        "--install",
        nargs="+",
        metavar="PKG",
        help="Port and install kernel-family package(s) (linux-cachyos*)",
    )
    action.add_argument(
        "--update", action="store_true", help="Update installed CachyOS kernel packages"
    )
    action.add_argument(
        "--clean", action="store_true", help="Remove local cachyport cache data"
    )
    action.add_argument(
        "--doctor",
        action="store_true",
        help="Run preflight checks for tools, repo access, and configuration",
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
        "--skip-signature-check",
        action="store_true",
        help="With --install/--update, skip detached signature verification",
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
        if args.doctor:
            return handle_doctor(args, color_enabled)
        raise RuntimeError("no action selected")
    except KeyboardInterrupt:
        print_error("interrupted by user", color_enabled)
        return 130
    except Exception as exc:
        print_error(str(exc), color_enabled)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
