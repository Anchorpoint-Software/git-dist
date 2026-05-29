#!/usr/bin/env python3
"""Build a portable Git distribution bundled with Anchorpoint's git-lfs fork.

Windows -> MinGit (built from the Git for Windows SDK) + the fork's git-lfs.exe.
macOS   -> git built from source (arm64 or x86_64) + the fork's git-lfs.

The fork's git-lfs binary is dropped into the distribution's libexec/git-core/
so that `git lfs ...` resolves to it via GIT_EXEC_PATH. The result is staged
under dist/<platform>/ and, with --package, zipped to dist/git-<os>-<arch>.zip
plus a .sha256 sidecar for publishing as a GitHub release asset.

Adapted from Anchorpoint's earlier desktop Git build pipeline.
Differences: config/env-driven (no interactive prompts), output goes to the
gitignored dist/ instead of committed trees, packaging is built in, and the
git-lfs fork defaults to the bundled third_party/git-lfs submodule.

Usage:
    python build.py [--package] [--nosign] [--arch arm64|x86_64]

Paths come from config.ini (copy config.example.ini) or env vars
GIT_SDK_PATH / GIT_LFS_PATH / GIT_SOURCE_PATH (env wins).
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import platform
import shutil
import zipfile

ROOT = os.path.dirname(os.path.realpath(__file__))
MACOSX_DEPLOYMENT_TARGET = "12.0"

# Server-side and unsupported programs we never ship.
PRUNE_PROGRAMS = [
    "bin/git-cvsserver",
    "bin/git-receive-pack",
    "bin/git-upload-archive",
    "bin/git-upload-pack",
    "bin/git-shell",
    "libexec/git-core/git-svn",
    "libexec/git-core/git-p4",
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(os.path.join(ROOT, "config.ini"))
    return config


def resolve_path(config, section: str, env_var: str, *, required: bool) -> str:
    """Env var wins over config.ini. Errors (never prompts) when required and missing."""
    value = os.environ.get(env_var) or (
        config[section]["path"] if config.has_option(section, "path") else ""
    )
    if value and section == "gitlfs" and not os.path.isabs(value):
        value = os.path.join(ROOT, value)
    if required and (not value or not os.path.exists(value)):
        raise SystemExit(
            f"[git-dist] {section} path not found "
            f"(set [{section}].path in config.ini or ${env_var}): {value!r}"
        )
    return value


# --------------------------------------------------------------------------- #
# git-lfs fork (Go build)
# --------------------------------------------------------------------------- #
def build_gitlfs(gitlfs: str, dest: str, goos: str, goarch: str) -> None:
    """Build the fork's git-lfs and drop it into the distribution's git-core."""
    print(f"Building git-lfs fork ({goos}/{goarch}) from {gitlfs}")
    out_name = "git-lfs.exe" if goos == "windows" else "git-lfs"
    out_path = os.path.join(gitlfs, "bin", out_name)

    env = os.environ.copy()
    env["GOOS"] = goos
    env["GOARCH"] = goarch
    # -trimpath drops local paths; -s -w strips debug info for a smaller binary.
    code = _run(
        ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", out_path, "."],
        cwd=gitlfs,
        env=env,
    )
    if code != 0:
        raise SystemExit("[git-dist] git-lfs build failed")

    git_core = git_core_dir(dest)
    os.makedirs(git_core, exist_ok=True)
    target = os.path.join(git_core, out_name)
    shutil.copy2(out_path, target)
    if goos != "windows":
        os.chmod(target, 0o755)
    print(f"git-lfs installed at {target}")


# --------------------------------------------------------------------------- #
# Git (Windows: MinGit via Git SDK)
# --------------------------------------------------------------------------- #
def build_git_windows(gitsdk: str, dest: str) -> None:
    src_git = os.path.join(gitsdk, "usr/src/git")
    build_extra = os.path.join(gitsdk, "usr/src/build-extra")
    if not os.path.exists(src_git) or not os.path.exists(build_extra):
        raise SystemExit(
            f"[git-dist] Git SDK not initialized. Open {gitsdk}/git-bash.exe and run "
            "'sdk cd git && sdk init git' and 'sdk cd build-extra && sdk init build-extra'."
        )

    print("Building Git (Windows)")
    os.environ["PATH"] = f"{gitsdk}/usr/bin;{os.environ['PATH']}"
    if _run_shell(
        f'{gitsdk}/git-bash.exe -c \'cd {gitsdk}/usr/src/git; '
        f'make install CFLAGS="-O3 -DNDEBUG -Wno-error"\''
    ) != 0:
        raise SystemExit("[git-dist] git build failed")

    print("Building MinGit (anchorpoint flavor)")
    if _run_shell(
        f"{gitsdk}/git-bash.exe -c 'cd {gitsdk}/usr/src/build-extra/mingit; "
        f"sh release.sh --output={gitsdk}/usr/src/build-extra/build anchorpoint'"
    ) != 0:
        raise SystemExit("[git-dist] MinGit build failed")

    zip_path = os.path.join(
        gitsdk, "usr/src/build-extra/build/MinGit-anchorpoint-64-bit.zip"
    )
    if not os.path.exists(zip_path):
        raise SystemExit(f"[git-dist] MinGit zip not produced: {zip_path}")
    print("Extracting MinGit")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


# --------------------------------------------------------------------------- #
# Git (macOS: from source)
# --------------------------------------------------------------------------- #
def build_git_macos(git_source: str, dest: str, arch: str) -> None:
    host_cpu = "arm64" if arch == "arm64" else "x86_64"
    target = f"-target {host_cpu}-apple-darwin"
    print(f"Building Git for macOS ({host_cpu})")

    _run(["make", "clean"], cwd=git_source)
    script = f"""#!/bin/bash
set -e
unset LIBRARY_PATH CPATH PKG_CONFIG_PATH
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export LDFLAGS="-L/usr/lib"
export CPPFLAGS="-I/usr/include"
DESTDIR="{dest}" make strip install prefix=/ \\
  CFLAGS="-I/usr/include {target} -O3 -DNDEBUG" \\
  LDFLAGS="-L/usr/lib" \\
  HOST_CPU="{host_cpu}" \\
  CURL_CONFIG=/usr/bin/curl-config \\
  NO_PERL=1 NO_TCLTK=1 NO_GETTEXT=1 NO_DARWIN_PORTS=1 \\
  NO_INSTALL_HARDLINKS=1 \\
  MACOSX_DEPLOYMENT_TARGET={MACOSX_DEPLOYMENT_TARGET}
"""
    script_path = os.path.join(ROOT, "build-temp", "git_build.sh")
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    if _run(["bash", script_path], cwd=git_source) != 0:
        raise SystemExit("[git-dist] git build failed")


# --------------------------------------------------------------------------- #
# Distribution hygiene + config
# --------------------------------------------------------------------------- #
def prune_programs(dest: str) -> None:
    for rel in PRUNE_PROGRAMS:
        for base in (dest, os.path.join(dest, "mingw64")):
            p = os.path.join(base, rel)
            if os.path.exists(p):
                os.remove(p)


def patch_git_config(dest: str) -> None:
    """Bake the system-level config the app relies on into the bundled gitconfig.

    Mirrors dugite-native: portable-friendly, schannel TLS on Windows.
    """
    candidates = [
        os.path.join(dest, "etc/gitconfig"),
        os.path.join(dest, "mingw64/etc/gitconfig"),
    ]
    system_config = next((c for c in candidates if os.path.exists(c)), None)
    if not system_config:
        print("[git-dist] WARNING: no bundled gitconfig found to patch")
        return

    pairs = [
        ("core.symlinks", "false"),
        ("core.autocrlf", "true"),
        ("core.fscache", "true"),
        ("http.sslBackend", "schannel"),
        ("http.schannelUseSSLCAInfo", "false"),
        ("credential.https://dev.azure.com.useHttpPath", "true"),
    ]
    for key, value in pairs:
        _run(["git", "config", "--file", system_config, key, value])
    _run(["git", "config", "--file", system_config, "--unset", "http.sslCAInfo"])
    _run(["git", "config", "--file", system_config, "--remove-section", "include"])

    for rel in ("etc/gitattributes", "mingw64/etc/gitattributes"):
        p = os.path.join(dest, rel)
        if os.path.exists(p):
            os.remove(p)
    legacy = os.path.join(dest, "mingw64/bin/git-credential-wincred.exe")
    if os.path.exists(legacy):
        os.remove(legacy)


# --------------------------------------------------------------------------- #
# Signing (config/secret-driven; no-op without identities)
# --------------------------------------------------------------------------- #
def sign(dest: str) -> None:
    if platform.system() == "Darwin":
        identity = os.environ.get("MACOS_SIGN_IDENTITY")
        if not identity:
            print("[git-dist] WARNING: MACOS_SIGN_IDENTITY unset — skipping signing")
            return
        for binary in _executables(dest):
            _run([
                "codesign", "--deep", "--force", "--verify", "--verbose",
                "--timestamp", "--options", "runtime", "--sign", identity, binary,
            ])
        print("macOS signing complete (notarization happens in CI after packaging)")
    elif platform.system() == "Windows":
        script = os.environ.get("WINDOWS_SIGN_SCRIPT")
        if not script or not os.path.exists(script):
            print("[git-dist] WARNING: WINDOWS_SIGN_SCRIPT unset — skipping signing")
            return
        _run_shell(f'powershell -File "{script}" -folderPath "{dest}"')
        print("Windows signing complete")


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def package(dest: str, asset_name: str) -> None:
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    zip_path = os.path.join(ROOT, "dist", f"{asset_name}.zip")
    print(f"Packaging {dest} -> {zip_path}")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, _dirs, files in os.walk(dest):
            for name in files:
                abs_path = os.path.join(folder, name)
                arc = os.path.relpath(abs_path, dest)
                zf.write(abs_path, arc)
    digest = _sha256(zip_path)
    with open(zip_path + ".sha256", "w") as f:
        f.write(f"{digest}  {asset_name}.zip\n")
    print(f"sha256 {digest}")


# --------------------------------------------------------------------------- #
# Publishing (local: each build host uploads its own assets to the shared tag)
# --------------------------------------------------------------------------- #
def built_git_version(dest: str) -> str:
    """Read the upstream Git version (e.g. "2.47.0") from the freshly-built git."""
    candidates = [
        os.path.join(dest, "cmd", "git.exe"),
        os.path.join(dest, "mingw64", "bin", "git.exe"),
        os.path.join(dest, "bin", "git"),
    ]
    gitbin = next((c for c in candidates if os.path.exists(c)), None)
    if not gitbin:
        raise SystemExit("[git-dist] cannot find built git to derive the version")
    out = _capture([gitbin, "--version"])  # "git version 2.47.0[.windows.1]"
    token = out.split()[-1] if out.split() else ""
    nums = []
    for part in token.split("."):
        if part.isdigit():
            nums.append(part)
        else:
            break
    if len(nums) < 3:
        raise SystemExit(f"[git-dist] could not parse git version from {out!r}")
    return ".".join(nums[:3])


def compute_tag(dest: str, bump: bool) -> str:
    """Auto-derive v<gitver>.anchorpoint.<n> (GfW-style).  <n> reuses the highest
    existing build for this Git version, or starts at 1; `bump` forces a new
    number for a re-cut of the same Git version."""
    gitver = built_git_version(dest)
    base = f"v{gitver}.anchorpoint"
    out = _capture([
        "gh", "release", "list", "--limit", "200",
        "--json", "tagName", "--jq", ".[].tagName",
    ])
    prefix = base + "."
    existing = sorted(
        int(line[len(prefix):])
        for line in out.splitlines()
        if line.startswith(prefix) and line[len(prefix):].isdigit()
    )
    if not existing:
        n = 1
    elif bump:
        n = existing[-1] + 1
    else:
        n = existing[-1]
    tag = f"{base}.{n}"
    print(f"[git-dist] auto tag: {tag} (git {gitver}; existing builds: {existing or 'none'})")
    return tag


def publish(tag: str, asset: str) -> None:
    files = [
        os.path.join(ROOT, "dist", f"{asset}.zip"),
        os.path.join(ROOT, "dist", f"{asset}.zip.sha256"),
    ]
    for f in files:
        if not os.path.exists(f):
            raise SystemExit(f"[git-dist] missing {f} -- run with --package first")
    # Create the release on first publish, then upload (clobbering a prior asset
    # of the same name) so the Windows and macOS hosts can each add their
    # archives to the same tag.
    if _run(["gh", "release", "view", tag]) == 0:
        ok = _run(["gh", "release", "upload", tag, "--clobber", *files]) == 0
    else:
        ok = _run([
            "gh", "release", "create", tag, "--title", tag,
            "--notes", "Portable Git + Anchorpoint git-lfs fork.", *files,
        ]) == 0
    if not ok:
        raise SystemExit(f"[git-dist] gh release publish failed for {tag}")
    print(f"[git-dist] published {asset} -> release {tag}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def git_core_dir(dest: str) -> str:
    win = os.path.join(dest, "mingw64/libexec/git-core")
    return win if platform.system() == "Windows" else os.path.join(dest, "libexec/git-core")


def fresh_dest(asset_name: str) -> str:
    dest = os.path.join(ROOT, "dist", asset_name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    return dest


def asset_name_for(arch: str) -> str:
    if platform.system() == "Windows":
        return "git-windows-x64"
    return f"git-macos-{'arm64' if arch == 'arm64' else 'x64'}"


def _executables(dest: str):
    for sub in ("bin", "libexec/git-core"):
        d = os.path.join(dest, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and (os.access(p, os.X_OK) or name.endswith(".dylib")):
                yield p


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(args, cwd=None, env=None) -> int:
    import subprocess
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.call(args, cwd=cwd, env=env, **kwargs)


def _capture(args, cwd=None) -> str:
    import subprocess
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.check_output(
            args, cwd=cwd, text=True, stderr=subprocess.DEVNULL, **kwargs
        )
    except Exception:
        return ""


def _run_shell(cmd: str) -> int:
    return os.system(cmd)


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Build portable Git + Anchorpoint git-lfs fork")
    parser.add_argument("--package", action="store_true", help="zip the result + emit .sha256")
    parser.add_argument("--nosign", action="store_true", help="skip code signing")
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=None,
                        help="macOS target arch (defaults to host)")
    parser.add_argument("--publish", action="store_true",
                        help="after --package, create/upload the GitHub release via gh (auto-tagged)")
    parser.add_argument("--tag", default=None,
                        help="explicit release tag (default: auto v<gitver>.anchorpoint.<n>)")
    parser.add_argument("--bump", action="store_true",
                        help="with auto-tag, start a new build number for this git version (re-cut)")
    args = parser.parse_args()

    config = load_config()
    gitlfs = resolve_path(config, "gitlfs", "GIT_LFS_PATH", required=True)
    arch = args.arch or ("arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64")
    asset = asset_name_for(arch)
    dest = fresh_dest(asset)

    if platform.system() == "Windows":
        gitsdk = resolve_path(config, "gitsdk", "GIT_SDK_PATH", required=True)
        build_git_windows(gitsdk, dest)
        build_gitlfs(gitlfs, dest, goos="windows", goarch="amd64")
    elif platform.system() == "Darwin":
        git_source = resolve_path(config, "gitsource", "GIT_SOURCE_PATH", required=True)
        build_git_macos(git_source, dest, arch)
        build_gitlfs(gitlfs, dest, goos="darwin", goarch="amd64" if arch == "x86_64" else "arm64")
    else:
        raise SystemExit("[git-dist] unsupported platform (Windows and macOS only)")

    prune_programs(dest)
    patch_git_config(dest)
    if not args.nosign:
        sign(dest)
    if args.package:
        package(dest, asset)
    if args.publish:
        if not args.package:
            raise SystemExit("[git-dist] --publish requires --package")
        tag = args.tag or compute_tag(dest, args.bump)
        publish(tag, asset)
    print(f"[git-dist] done -> {dest}")


if __name__ == "__main__":
    main()
