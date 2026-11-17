#!/usr/bin/env python3
"""Project CI commands; every archive fixture is synthetic and capture stays off."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "tools/ci.toml").read_text())
LIMITS = CONFIG["fixture"]
for value in LIMITS.values():
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("CI fixture limits must be finite and positive")
PACKAGE = tomllib.loads((ROOT / "daemon/Cargo.toml").read_text())["package"]
OS_NAME = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}[platform.system()]
ARCH = "ARM64" if platform.machine().lower() in ("arm64", "aarch64") else "X64"


def output(name: str) -> Path:
    path = ROOT
    for part in PurePosixPath(name).parts:
        if part in (".", "..") or "/" in part or "\\" in part:
            raise ValueError("CI output must remain in its workspace")
        path = path / part
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise ValueError("CI output directory must not be redirected")
        path.mkdir(exist_ok=True)
    return path


def environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RLDYOUR_CLIPBOARD_")
        and key not in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES")
    }
    env["PYTHONPATH"] = str(ROOT / "python/src")
    return env


def run(
    *args: str, env: dict[str, str] | None = None, timeout: float | None = None
) -> None:
    print("+", shlex.join(args), flush=True)
    subprocess.run(args, cwd=ROOT, env=env, timeout=timeout, check=True)


def captured(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def source() -> str:
    if captured("git", "status", "--porcelain"):
        raise ValueError("Qualification requires committed source")
    commit = captured("git", "rev-parse", "HEAD")
    if os.environ.get("GITHUB_SHA", commit) != commit:
        raise ValueError("Checkout differs from the CI source")
    return commit


def minimum_rust() -> str:
    version = PACKAGE["rust-version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version
    ):
        raise ValueError(
            "daemon/Cargo.toml must declare a numeric minimum Rust version"
        )
    return version + ".0" if version.count(".") == 1 else version


def setup() -> None:
    run("cargo", "fetch", "--locked", "--manifest-path", "daemon/Cargo.toml")
    if os.name != "nt":
        run(
            "uv",
            "sync",
            "--project",
            "python",
            "--locked",
            "--group",
            "test",
            "--no-install-project",
            "--no-build",
        )


def setup_platform() -> None:
    if sys.platform != "linux":
        return
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise ValueError(
            "System-package preparation is confined to an ephemeral public CI job"
        )
    # Retain the existing HTTPS route for the hosted Ubuntu 24 mirror failure.
    if 'VERSION_ID="24.04"' in Path("/etc/os-release").read_text():
        for path in [
            Path("/etc/apt/sources.list"),
            Path("/etc/apt/sources.list.d/ubuntu.sources"),
        ]:
            if path.exists():
                run(
                    "sudo",
                    "sed",
                    "-i",
                    "s|http://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g",
                    str(path),
                )
    settings = CONFIG["linux"]
    run(
        "sudo",
        "apt-get",
        "update",
        "-o",
        f"Acquire::Retries={settings['apt_retries']}",
        "-o",
        f"Acquire::http::Timeout={settings['apt_timeout_seconds']}",
        "-o",
        f"Acquire::https::Timeout={settings['apt_timeout_seconds']}",
    )
    run(
        "sudo",
        "apt-get",
        "install",
        "--no-install-recommends",
        "-y",
        *settings["packages"],
    )


def check() -> None:
    run("cargo", "fmt", "--check", "--manifest-path", "daemon/Cargo.toml")
    scripts = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "scripts").glob("*.sh")
    )
    run(
        "shellcheck",
        *scripts,
        "install.sh",
        "uninstall.sh",
        "install-macos.sh",
        "uninstall-macos.sh",
    )
    run("bash", "scripts/check-version.sh")
    run("bash", "scripts/check-consistency.sh")
    for folder in [ROOT / "scripts", ROOT / "python"]:
        for path in folder.rglob("*.py"):
            if not any(
                part in (".venv", "dist", "build", "__pycache__")
                for part in path.relative_to(folder).parts
            ):
                ast.parse(path.read_text(), filename=str(path))


def test_extension() -> None:
    run("bash", "scripts/check-extension.sh")
    run("gjs", "-m", "extension/tests/smoke.js")
    with tempfile.TemporaryDirectory(prefix="gjs-") as directory:
        run(
            "gjs",
            "-m",
            "extension/tests/framing.js",
            env=dict(environment(), XDG_RUNTIME_DIR=directory),
            timeout=LIMITS["command_seconds"],
        )


def binary() -> Path:
    return (
        ROOT
        / "daemon/target/release"
        / (PACKAGE["name"] + (".exe" if os.name == "nt" else ""))
    )


@contextmanager
def fixture():
    base = (
        os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
        if os.name == "nt"
        else "/tmp"
    )
    with tempfile.TemporaryDirectory(prefix="cb-", dir=base) as directory:
        home = Path(directory).resolve()
        env = dict(
            environment(),
            RLDYOUR_CLIPBOARD_HOME=str(home),
            RLDYOUR_CLIPBOARD_CAPTURE="0",
        )
        with (output("build/ci") / "daemon.log").open("w+") as log:
            process = subprocess.Popen(
                [str(binary())], cwd=ROOT, env=env, stdout=log, stderr=log
            )
            try:
                if os.name != "nt":
                    sys.path.insert(0, str(ROOT / "python/src"))
                    from rldyour_clipboard import Client

                    deadline = time.monotonic() + LIMITS["startup_seconds"]
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(
                                "Owned fixture daemon exited before readiness"
                            )
                        try:
                            with Client(
                                path=home / "rldyour-clipboard.sock", watch=False
                            ):
                                break
                        except (FileNotFoundError, ConnectionRefusedError):
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "Owned fixture daemon did not become ready"
                                ) from None
                            time.sleep(LIMITS["poll_seconds"])
                yield env, home
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=LIMITS["shutdown_seconds"])
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=LIMITS["shutdown_seconds"])


def swift_tests() -> None:
    folder = output("build/ci")
    files = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "macos").glob("*.swift")
    )
    run(
        "swiftc",
        "-target",
        f"{platform.machine()}-apple-macosx12.0",
        "-swift-version",
        "6",
        "-parse-as-library",
        "-warnings-as-errors",
        "-O",
        "-framework",
        "AppKit",
        *files,
        "-o",
        str(folder / "clipboard-app"),
    )
    run(
        "swiftc",
        "-swift-version",
        "6",
        "-parse-as-library",
        "-warnings-as-errors",
        "macos/Models.swift",
        "macos/tests/ModelsTests.swift",
        "-o",
        str(folder / "model-tests"),
    )
    run(str(folder / "model-tests"))
    plists = [
        str(path.relative_to(ROOT))
        for part in ["macos", "daemon/launchd"]
        for path in (ROOT / part).glob("*.plist")
    ]
    run("plutil", "-lint", *plists)


def client_digest() -> str | None:
    return digest(ROOT / "build/ci/clipboard-app") if sys.platform == "darwin" else None


def tests() -> None:
    commit = source()
    run(
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "scripts/tests",
        env=environment(),
    )
    manifest = ("--manifest-path", "daemon/Cargo.toml", "--locked")
    for features in ("--all-features", "--no-default-features"):
        run(
            "cargo",
            "clippy",
            *manifest,
            "--all-targets",
            features,
            "--",
            "-D",
            "warnings",
        )
        run("cargo", "test", *manifest, features)
    # Check the no-thumbnail binary too, then retain the normal variant for IPC.
    run("cargo", "build", *manifest, "--release", "--no-default-features")
    run("cargo", "build", *manifest, "--release", "--all-features")
    if os.name != "nt":
        run(
            "uv",
            "run",
            "--project",
            "python",
            "--locked",
            "--no-sync",
            "--group",
            "test",
            "pytest",
            "python/tests",
            env=environment(),
        )
    if sys.platform == "darwin":
        swift_tests()
    with fixture() as (env, home):
        if os.name == "nt":
            run(
                "pwsh",
                "-NoProfile",
                "-File",
                "scripts/e2e-sample.ps1",
                "-StartupTimeoutMilliseconds",
                str(round(1000 * LIMITS["startup_seconds"])),
                "-RetryIntervalMilliseconds",
                str(round(1000 * LIMITS["poll_seconds"])),
                env=env,
                timeout=LIMITS["command_seconds"],
            )
        else:
            run(
                sys.executable,
                "scripts/e2e-sample.py",
                env=env,
                timeout=LIMITS["command_seconds"],
            )
            if sys.platform == "linux":
                run(
                    "gjs",
                    "-m",
                    "extension/tests/client.js",
                    env=env,
                    timeout=LIMITS["command_seconds"],
                )
            else:
                sys.path.insert(0, str(ROOT / "python/src"))
                from rldyour_clipboard import Client

                with Client(
                    path=home / "rldyour-clipboard.sock", role="both", watch=False
                ) as client:
                    client.record([("text/plain", b"native socket synthetic")])
                    client.record(
                        [
                            (
                                "text/plain",
                                b"native socket synthetic large:"
                                + b"x" * (3 * 1024 * 1024),
                            )
                        ]
                    )
                target = output("build/ci") / "socket-tests"
                run(
                    "swiftc",
                    "-swift-version",
                    "6",
                    "-parse-as-library",
                    "-warnings-as-errors",
                    "macos/Models.swift",
                    "macos/SocketClient.swift",
                    "macos/tests/SocketTests.swift",
                    "-o",
                    str(target),
                )
                run(
                    str(target),
                    str(home / "rldyour-clipboard.sock"),
                    env=env,
                    timeout=LIMITS["command_seconds"],
                )
    if os.name != "nt":
        run(
            sys.executable,
            "scripts/retention-e2e.py",
            env=environment(),
            timeout=LIMITS["command_seconds"],
        )
    if sys.platform == "linux":
        run(
            "cargo",
            "deny",
            "--manifest-path",
            "daemon/Cargo.toml",
            "--locked",
            "--all-features",
            "check",
            "advisories",
        )
    result = {
        "source_commit": commit,
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "platform": OS_NAME,
        "architecture": ARCH,
        "binary_sha256": digest(binary()),
        "client_binary_sha256": client_digest(),
    }
    (output("build/ci") / "tests.json").write_text(json.dumps(result, indent=2) + "\n")
    build()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def member_name(name: str) -> str:
    name = name.removeprefix("./")
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise ValueError("Archive member must remain relative")
    return str(path)


def archive_files(path: Path) -> dict[str, tuple[str, int]]:
    found = {}
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = member_name(member.name)
                if member.mode & 0o7000:
                    raise ValueError("Archive member has unsafe permissions")
                if member.isdir():
                    continue
                if (
                    name == "."
                    or name in found
                    or not member.isfile()
                    or member.mode & 0o7000
                ):
                    raise ValueError("Archive contains duplicate or nonregular members")
                stream = archive.extractfile(member)
                assert stream is not None
                found[name] = (
                    hashlib.file_digest(stream, "sha256").hexdigest(),
                    member.mode & 0o777,
                )
    else:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member_name(member.filename)
                mode = member.external_attr >> 16
                expected_kind = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
                if mode & 0o7000 or stat.S_IFMT(mode) not in (0, expected_kind):
                    raise ValueError("Archive member has unsafe permissions or type")
                if member.is_dir():
                    continue
                if name == "." or name in found:
                    raise ValueError("Archive contains duplicate or nonregular members")
                with archive.open(member) as stream:
                    found[name] = (
                        hashlib.file_digest(stream, "sha256").hexdigest(),
                        mode & 0o777,
                    )
    return found


def regular_files(folder: Path) -> dict[str, tuple[str, int]]:
    found = {}
    for path in folder.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Package staging contains a redirected or special entry")
        if path.is_file():
            found[path.relative_to(folder).as_posix()] = (
                digest(path),
                path.stat().st_mode & 0o777,
            )
    return found


def copy_file(source_path: Path, destination: Path) -> None:
    if (
        source_path.is_symlink()
        or not source_path.is_file()
        or not source_path.resolve().is_relative_to(ROOT)
    ):
        raise ValueError("Package source must be a regular workspace file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)


def package_archive(stage: Path, path: Path) -> dict:
    expected = regular_files(stage)
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "w:gz") as archive:
            archive.add(stage, arcname=".")
    else:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(expected):
                archive.write(stage / name, name)
    actual = archive_files(path)
    if actual != expected:
        raise ValueError(
            "Archive bytes, complete contents or modes differ from staging"
        )
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest(path),
        "members": [
            {"name": name, "sha256": value[0], "mode": oct(value[1])}
            for name, value in sorted(actual.items())
        ],
    }


def reconcile_distribution(
    folder: Path, commit: str, version: str, binary_sha256: str
) -> None:
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Qualified output directory must be regular")
    suffix = OS_NAME.lower() + "-" + ARCH.lower()
    qualification_name = f"qualification-{suffix}.json"
    metadata_name = f"release-metadata-{suffix}.json"
    for name in (qualification_name, metadata_name):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Qualification metadata must be regular files")
    receipt = json.loads((folder / qualification_name).read_text())
    metadata = json.loads((folder / metadata_name).read_text())
    if (
        receipt["source_commit"] != commit
        or receipt["version"] != version
        or receipt["platform"] != OS_NAME
        or receipt["architecture"] != ARCH
        or receipt["binary_sha256"] != binary_sha256
        or receipt["client_binary_sha256"] != client_digest()
        or metadata
        != {
            "schema_version": 1,
            "source_commit": commit,
            "version": version,
            "platform": OS_NAME,
            "architecture": ARCH,
        }
    ):
        raise ValueError("Existing qualification belongs to different tested inputs")
    expected = {qualification_name, metadata_name}
    for archive in receipt["archives"]:
        name = member_name(archive["name"])
        if "/" in name or name in expected:
            raise ValueError("Qualified files must be unique top-level entries")
        expected.add(name)
        path = folder / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != archive["bytes"]
            or digest(path) != archive["sha256"]
        ):
            raise ValueError("Existing qualified archive changed")
        members = [
            {"name": name, "sha256": value[0], "mode": oct(value[1])}
            for name, value in sorted(archive_files(path).items())
        ]
        if members != archive["members"]:
            raise ValueError("Existing archive contents or modes changed")
    actual = {path.name for path in folder.iterdir()}
    if actual != expected:
        raise ValueError(
            f"Qualification files differ: extra={sorted(actual - expected)}, "
            f"missing={sorted(expected - actual)}"
        )


def build() -> None:
    commit = source()
    record = json.loads((ROOT / "build/ci/tests.json").read_text())
    if record != {
        "source_commit": commit,
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "platform": OS_NAME,
        "architecture": ARCH,
        "binary_sha256": digest(binary()),
        "client_binary_sha256": client_digest(),
    }:
        raise ValueError("Packaging requires this exact job's tested binary")
    destination = ROOT / "dist"
    if destination.exists():
        reconcile_distribution(
            destination, commit, PACKAGE["version"], digest(binary())
        )
        return
    with tempfile.TemporaryDirectory(
        prefix="qualified-", dir=output("build/ci")
    ) as temporary:
        dist = Path(temporary)
        version = PACKAGE["version"]
        archives = []
        with tempfile.TemporaryDirectory(
            prefix="package-", dir=output("build/ci")
        ) as temporary:
            stage = Path(temporary)
            for name in ["LICENSE", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"]:
                copy_file(ROOT / name, stage / name)
            if sys.platform == "linux":
                copy_file(binary(), stage / "prebuilt" / PACKAGE["name"])
                for name in ["install.sh", "uninstall.sh"]:
                    copy_file(ROOT / name, stage / name)
                shutil.copytree(
                    ROOT / "daemon/systemd", stage / "daemon/systemd", symlinks=True
                )
                shutil.copytree(
                    ROOT / "extension",
                    stage / "extension",
                    symlinks=True,
                    ignore=shutil.ignore_patterns("tests", "gschemas.compiled"),
                )
                run("glib-compile-schemas", str(stage / "extension/schemas"))
                archives.append(
                    package_archive(
                        stage, dist / f"rldyour-clipboard-{version}-x86_64-linux.tar.gz"
                    )
                )
                archives.append(
                    package_archive(
                        stage / "extension",
                        dist / f"rldyour-clipboard-gnome-{version}.zip",
                    )
                )
                run(
                    "uv",
                    "build",
                    "python",
                    "--no-create-gitignore",
                    "--out-dir",
                    str(dist),
                )
                for path in sorted(dist.glob("*.whl")) + sorted(dist.glob("*.tar.gz")):
                    if path.name.startswith("rldyour_clipboard-"):
                        members = archive_files(path)
                        if path.suffix == ".whl":
                            expected = {
                                str(file.relative_to(ROOT / "python/src")).replace(
                                    "\\", "/"
                                ): digest(file)
                                for file in (ROOT / "python/src").rglob("*.py")
                            }
                            if any(
                                name not in members or members[name][0] != value
                                for name, value in expected.items()
                            ):
                                raise ValueError(
                                    "Wheel differs from the checked Python source"
                                )
                        archives.append(
                            {
                                "name": path.name,
                                "bytes": path.stat().st_size,
                                "sha256": digest(path),
                                "members": [
                                    {
                                        "name": name,
                                        "sha256": value[0],
                                        "mode": oct(value[1]),
                                    }
                                    for name, value in sorted(members.items())
                                ],
                            }
                        )
            elif sys.platform == "darwin":
                label = plistlib.loads(
                    (ROOT / "macos/io.nddev.rldyour-clipboard.plist").read_bytes()
                )["Label"]
                name = Path(
                    plistlib.loads(
                        (ROOT / "macos/io.nddev.rldyour-clipboard.plist").read_bytes()
                    )["ProgramArguments"][0]
                ).name
                app = stage / "prebuilt" / (name + ".app")
                copy_file(
                    ROOT / "build/ci/clipboard-app", app / "Contents/MacOS" / name
                )
                (app / "Contents/Info.plist").write_bytes(
                    plistlib.dumps(
                        {
                            "CFBundleExecutable": name,
                            "CFBundleIdentifier": label,
                            "CFBundleName": name,
                            "CFBundleVersion": version,
                            "CFBundleShortVersionString": version,
                            "LSUIElement": True,
                        }
                    )
                )
                run("codesign", "--force", "--sign", "-", str(app))
                copy_file(binary(), stage / "prebuilt" / PACKAGE["name"])
                shutil.copytree(
                    ROOT / "macos",
                    stage / "macos",
                    symlinks=True,
                    ignore=shutil.ignore_patterns("*.swift", "tests"),
                )
                shutil.copytree(
                    ROOT / "daemon/launchd", stage / "daemon/launchd", symlinks=True
                )
                for name in ["install-macos.sh", "uninstall-macos.sh"]:
                    copy_file(ROOT / name, stage / name)
                (stage / "VERSION").write_text(version + "\n")
                arch = "arm64" if ARCH == "ARM64" else "x86_64"
                archives.append(
                    package_archive(
                        stage, dist / f"rldyour-clipboard-{version}-{arch}-macos.zip"
                    )
                )
            else:
                copy_file(binary(), stage / binary().name)
                archives.append(
                    package_archive(
                        stage, dist / f"rldyour-clipboard-{version}-x86_64-windows.zip"
                    )
                )
        suffix = OS_NAME.lower() + "-" + ARCH.lower()
        qualification = {
            "schema_version": 1,
            "source_commit": commit,
            "version": version,
            "platform": OS_NAME,
            "architecture": ARCH,
            "rust": captured("rustc", "--version"),
            "image_version": os.environ.get("ImageVersion"),
            "archives": archives,
            "checks": ["native feature sets", "archive boundaries", "synthetic IPC"]
            + (["Python client", "retention/restart"] if os.name != "nt" else [])
            + (
                ["GNOME client", "dependency advisories"]
                if sys.platform == "linux"
                else []
            )
            + (
                ["Swift models", "macOS client compile/socket"]
                if sys.platform == "darwin"
                else []
            )
            + (["Windows protocol/pin/restore"] if os.name == "nt" else []),
            "unperformed": ["installed-user qualification", "physical recovery"],
            "binary_sha256": digest(binary()),
            "client_binary_sha256": client_digest(),
        }
        (dist / f"qualification-{suffix}.json").write_text(
            json.dumps(qualification, indent=2) + "\n"
        )
        metadata = {
            "schema_version": 1,
            "source_commit": commit,
            "version": version,
            "platform": OS_NAME,
            "architecture": ARCH,
        }
        (dist / f"release-metadata-{suffix}.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )
        reconcile_distribution(dist, commit, version, digest(binary()))
        dist.rename(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "setup",
            "setup-platform",
            "check",
            "test",
            "test-extension",
            "build",
            "minimum-rust",
            "check-minimum-rust",
        ],
    )
    command = parser.parse_args().command
    if command == "minimum-rust":
        print(minimum_rust())
    elif command == "check-minimum-rust":
        run(
            "rustup",
            "run",
            minimum_rust(),
            "cargo",
            "check",
            "--locked",
            "--manifest-path",
            "daemon/Cargo.toml",
        )
    else:
        {
            "setup": setup,
            "setup-platform": setup_platform,
            "check": check,
            "test": tests,
            "test-extension": test_extension,
            "build": build,
        }[command]()


if __name__ == "__main__":
    main()
