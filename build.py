#!/usr/bin/env python3
"""Build a portable Git distribution bundled with Anchorpoint's git-lfs fork.

Windows -> MinGit (built from the Git for Windows SDK) + the fork's git-lfs.exe.
macOS   -> git built from source (arm64 or x86_64) + the fork's git-lfs + GCM.
Linux   -> git built from source (x64 or arm64) + the fork's git-lfs + GCM.

The fork's git-lfs binary is dropped into the distribution's libexec/git-core/
so that `git lfs ...` resolves to it via GIT_EXEC_PATH. On macOS/Linux the
pinned Git Credential Manager release is bundled under gcm/ and wired via a
baked etc/gitconfig (`credential.helper = manager`) — MinGit already ships GCM
on Windows. The result is staged
under dist/<platform>/ and, with --package, archived to
dist/git-<os>-<arch>.zip (Windows/macOS) or .tar.gz (Linux — GNU tar can't
read zips, and consumers extract with the platform tar) plus a .sha256
sidecar for publishing as a GitHub release asset.

Adapted from Anchorpoint's earlier desktop Git build pipeline.
Differences: config/env-driven (no interactive prompts), output goes to the
gitignored dist/ instead of committed trees, packaging is built in, and the
git-lfs fork defaults to the bundled third_party/git-lfs submodule.

Usage:
    python build.py [--package] [--nosign] [--publish] [--arch arm64|x86_64]
    release.ps1                  # Windows: verify SimplySign, then build+sign+publish

Paths come from config.ini (copy config.example.ini) or env vars
GIT_SDK_PATH / GIT_LFS_PATH / GIT_SOURCE_PATH (env wins). On Windows the SDK's
git/build-extra sources are auto-initialized on first build, and signing
defaults to ./sign-windows.ps1.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import platform
import shutil
import stat
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.realpath(__file__))
MACOSX_DEPLOYMENT_TARGET = "12.0"

# Server-side and unsupported programs we never ship. Entries are tried under
# <dest> and <dest>/mingw64, bare and with .exe (see prune_programs).
PRUNE_PROGRAMS = [
    "bin/git-cvsserver",
    "bin/git-receive-pack",
    "bin/git-upload-archive",
    "bin/git-upload-pack",
    "bin/git-shell",
    "libexec/git-core/git-svn",
    "libexec/git-core/git-p4",
    # Legacy Windows credential helper superseded by GCM; MinGit drops a copy
    # in both bin/ and libexec/git-core/.
    "bin/git-credential-wincred",
    "libexec/git-core/git-credential-wincred",
]

# Git Credential Manager, bundled on macOS/Linux (MinGit already ships GCM on
# Windows). Official self-contained release tarballs from
# git-ecosystem/git-credential-manager, pinned by version + per-asset sha256.
GCM_VERSION = "2.8.0"
GCM_SHA256 = {
    "osx-arm64": "a61399385e861b8e6e896d04e528eebe1214e002edbb98373e5a458dee194d56",
    "osx-x64": "bba2b1a20198b2a1a69496a7cd81f4cb23f569fcc7d17751a8a93c071bb6a2b9",
    "linux-arm64": "6e1782b351eec3399db43699ff4fe2a1bf576654310ee4580aa4867705c0bc08",
    "linux-x64": "6040c849b428a4e5b78594fd4fd2819fc3ea4ca472c13580b65a486bef9df21d",
}


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
# Git Credential Manager (macOS/Linux; Windows gets GCM via MinGit)
# --------------------------------------------------------------------------- #
def bundle_gcm(dest: str, arch: str) -> None:
    """Bundle the official GCM release so `credential.helper = manager` works.

    Downloads the pinned self-contained tarball (sha256-verified, cached in
    build-temp/), extracts it to <dest>/gcm/, and installs a small sh shim at
    libexec/git-core/git-credential-manager. git prepends GIT_EXEC_PATH to a
    helper's PATH, so the shim resolves exactly like the bundled git-lfs.
    A shim rather than a symlink or moving the files into git-core: the macOS
    tarball is a ~200-file .NET layout that must stay next to its apphost, and
    the apphost misresolves its .dll when the executable path is a symlink.
    """
    plat = "osx" if platform.system() == "Darwin" else "linux"
    key = f"{plat}-{'arm64' if arch == 'arm64' else 'x64'}"
    asset = f"gcm-{key}-{GCM_VERSION}.tar.gz"
    want = GCM_SHA256[key]

    cache = os.path.join(ROOT, "build-temp", asset)
    if not (os.path.exists(cache) and _sha256(cache) == want):
        url = (
            "https://github.com/git-ecosystem/git-credential-manager/"
            f"releases/download/v{GCM_VERSION}/{asset}"
        )
        print(f"Downloading {url}")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        urllib.request.urlretrieve(url, cache)
        got = _sha256(cache)
        if got != want:
            raise SystemExit(
                f"[git-dist] sha256 mismatch for {asset}\n"
                f"  expected {want}\n  got      {got}"
            )

    gcm_dir = os.path.join(dest, "gcm")
    shutil.rmtree(gcm_dir, ignore_errors=True)
    os.makedirs(gcm_dir)
    with tarfile.open(cache, "r:gz") as tf:
        try:
            tf.extractall(gcm_dir, filter="data")
        except TypeError:  # Python < 3.12: no extraction filters
            tf.extractall(gcm_dir)
    # GCM's own uninstaller doesn't apply to a bundled tree.
    uninstall = os.path.join(gcm_dir, "uninstall.sh")
    if os.path.exists(uninstall):
        os.remove(uninstall)
    os.chmod(os.path.join(gcm_dir, "git-credential-manager"), 0o755)

    shim = os.path.join(git_core_dir(dest), "git-credential-manager")
    os.makedirs(os.path.dirname(shim), exist_ok=True)
    with open(shim, "w", newline="\n") as f:
        f.write('#!/bin/sh\nexec "$(dirname "$0")/../../gcm/git-credential-manager" "$@"\n')
    os.chmod(shim, 0o755)
    print(f"GCM {GCM_VERSION} installed at {gcm_dir}")


# --------------------------------------------------------------------------- #
# Git (Windows: MinGit via Git SDK)
# --------------------------------------------------------------------------- #
def ensure_sdk_sources(gitsdk: str) -> None:
    """Fetch + checkout the SDK's git and build-extra sources if not yet present.

    git-sdk-64 ships usr/src/git and usr/src/build-extra as repos with their
    remote configured but no working tree (`sdk init` normally populates them).
    Do it here with plain git (shallow main) so a freshly-unpacked SDK builds
    without any manual steps.
    """
    for rel in ("usr/src/git", "usr/src/build-extra"):
        repo = os.path.join(gitsdk, rel)
        if not os.path.exists(os.path.join(repo, ".git")):
            raise SystemExit(
                f"[git-dist] {rel} is not a git repo under {gitsdk}; "
                "reinstall the Git for Windows SDK."
            )
        if _capture(["git", "-C", repo, "rev-parse", "--verify", "HEAD"]).strip():
            continue  # already checked out
        print(f"[git-dist] initializing SDK source: {rel}")
        if _run(["git", "-C", repo, "fetch", "--depth=1", "origin",
                 "+refs/heads/main:refs/remotes/origin/main"]) != 0:
            raise SystemExit(f"[git-dist] failed to fetch {rel}")
        if _run(["git", "-C", repo, "checkout", "-B", "main", "origin/main"]) != 0:
            raise SystemExit(f"[git-dist] failed to checkout {rel}")


def build_git_windows(gitsdk: str, dest: str) -> None:
    ensure_sdk_sources(gitsdk)

    print("Building Git (Windows)")
    os.environ["PATH"] = f"{gitsdk}/usr/bin;{os.environ['PATH']}"
    if _sdk_bash(
        gitsdk,
        'cd /usr/src/git && make install CFLAGS="-O3 -DNDEBUG -Wno-error"',
    ) != 0:
        raise SystemExit("[git-dist] git build failed")

    print("Building MinGit (anchorpoint flavor)")
    if _sdk_bash(
        gitsdk,
        "mkdir -p /usr/src/build-extra/build && "
        "cd /usr/src/build-extra/mingit && "
        "sh release.sh --output=/usr/src/build-extra/build anchorpoint",
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


def bundle_less(gitsdk: str, dest: str) -> None:
    """Bundle the `less` pager (MinGit omits it) so git can page output.

    less.exe needs three MSYS runtime DLLs; msys-2.0.dll and msys-ncursesw6.dll
    already ship in MinGit, so only msys-pcre2-8-0.dll is added. The terminfo
    database is copied too so ncurses can read terminal capabilities. git's
    default pager is `less`; Anchorpoint disables paging per call with `git -P`
    for programmatic (captured) output so it never blocks on the pager.
    """
    usr_bin_src = os.path.join(gitsdk, "usr", "bin")
    usr_bin_dst = os.path.join(dest, "usr", "bin")
    os.makedirs(usr_bin_dst, exist_ok=True)
    for name in ("less.exe", "msys-pcre2-8-0.dll"):
        src = os.path.join(usr_bin_src, name)
        if not os.path.exists(src):
            raise SystemExit(f"[git-dist] cannot bundle pager: missing {src}")
        shutil.copy2(src, os.path.join(usr_bin_dst, name))
    terminfo_src = os.path.join(gitsdk, "usr", "share", "terminfo")
    terminfo_dst = os.path.join(dest, "usr", "share", "terminfo")
    if os.path.isdir(terminfo_src):
        shutil.copytree(terminfo_src, terminfo_dst, dirs_exist_ok=True)
    print("Bundled less pager + terminfo")


# --------------------------------------------------------------------------- #
# Git (macOS: from source)
# --------------------------------------------------------------------------- #
def build_git_macos(git_source: str, dest: str, arch: str) -> None:
    host_cpu = "arm64" if arch == "arm64" else "x86_64"
    target = f"-target {host_cpu}-apple-darwin"
    print(f"Building Git for macOS ({host_cpu})")

    _run(["make", "clean"], cwd=git_source)
    # RUNTIME_PREFIX: resolve gitexecdir/templates relative to the binary at
    # runtime (via _NSGetExecutablePath, auto-enabled for Darwin in
    # config.mak.uname) instead of baking in the absolute prefix=/ path. Without
    # it the portable dist reports a fixed //libexec/git-core and can't locate
    # its own git-core once extracted anywhere but /. MinGit always builds with
    # RUNTIME_PREFIX, which is why the Windows asset is already relocatable.
    #
    # SKIP_DASHED_BUILT_INS: don't install the ~145 dashed builtin aliases
    # (git-add, git-commit, ...) in libexec/git-core -- they're redundant copies
    # of the git binary (it dispatches builtins internally; callers only ever use
    # `git <cmd>`). Tauri's resource bundler dereferences symlinks, so shipping
    # them re-explodes into ~500 MB of copies inside the .app; omitting them keeps
    # git-core to its ~31 real helper programs (git-http-fetch, git-remote-https,
    # git-lfs, ...).
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
  SKIP_DASHED_BUILT_INS=YesPlease \\
  RUNTIME_PREFIX=YesPlease \\
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
# Git (Linux: from source)
# --------------------------------------------------------------------------- #
def build_git_linux(git_source: str, dest: str) -> None:
    print("Building Git for Linux")

    _run(["make", "clean"], cwd=git_source)
    # Same portable-build shape as macOS. RUNTIME_PREFIX resolves
    # gitexecdir/templates relative to the binary at runtime (config.mak.uname
    # wires /proc/self/exe for Linux) so the dist works from any extraction
    # path. SKIP_DASHED_BUILT_INS / NO_INSTALL_HARDLINKS: see the macOS
    # rationale above — keeps libexec/git-core to the real helper programs.
    # Needs: gcc/make, libcurl4-openssl-dev, zlib1g-dev, libexpat1-dev, gettext.
    script = f"""#!/bin/bash
set -e
DESTDIR="{dest}" make strip install prefix=/ \\
  CFLAGS="-O3 -DNDEBUG" \\
  NO_PERL=1 NO_TCLTK=1 NO_GETTEXT=1 \\
  NO_INSTALL_HARDLINKS=1 \\
  SKIP_DASHED_BUILT_INS=YesPlease \\
  RUNTIME_PREFIX=YesPlease
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
            # On Windows these are .exe; PRUNE_PROGRAMS lists the bare names so
            # the same list works for macOS. Try both so the server-side
            # programs are actually removed on Windows too.
            for p in (os.path.join(base, rel), os.path.join(base, rel) + ".exe"):
                if os.path.exists(p):
                    os.remove(p)


def patch_git_config(dest: str) -> None:
    """Bake the system-level config the app relies on into the bundled gitconfig.

    Windows patches the gitconfig MinGit already ships (portable-friendly,
    schannel TLS — mirrors dugite-native). The macOS/Linux dists are built from
    plain git source and ship no gitconfig at all, so create etc/gitconfig —
    the RUNTIME_PREFIX build reads it relative to the extracted tree — and bake
    the wiring for the bundled GCM plus the lfs filter (MinGit's gitconfig
    already carries both on Windows).
    """
    if platform.system() == "Windows":
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
    else:
        # Created fresh: only keys that make sense off-Windows (no autocrlf/
        # symlinks/fscache, no schannel — the OpenSSL/SecureTransport builds
        # find the CA bundle via their own defaults).
        system_config = os.path.join(dest, "etc", "gitconfig")
        os.makedirs(os.path.dirname(system_config), exist_ok=True)
        pairs = [
            ("credential.helper", "manager"),
            ("credential.https://dev.azure.com.useHttpPath", "true"),
            # What `git lfs install --system` would write; MinGit bakes the
            # same block on Windows.
            ("filter.lfs.clean", "git-lfs clean -- %f"),
            ("filter.lfs.smudge", "git-lfs smudge -- %f"),
            ("filter.lfs.process", "git-lfs filter-process"),
            ("filter.lfs.required", "true"),
        ]
        if platform.system() == "Linux":
            # GCM has no default credential store on Linux. Secret Service is
            # what the GNOME/KDE keyrings implement; unusual setups can
            # override via GCM_CREDENTIAL_STORE.
            pairs.append(("credential.credentialStore", "secretservice"))

    for key, value in pairs:
        _run(["git", "config", "--file", system_config, key, value])

    if platform.system() == "Windows":
        _run(["git", "config", "--file", system_config, "--unset", "http.sslCAInfo"])
        _run(["git", "config", "--file", system_config, "--remove-section", "include"])
        for rel in ("etc/gitattributes", "mingw64/etc/gitattributes"):
            p = os.path.join(dest, rel)
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------------- #
# Signing (config/secret-driven; no-op without identities)
# --------------------------------------------------------------------------- #
def sign(dest: str) -> None:
    if platform.system() == "Darwin":
        identity = os.environ.get("MACOS_SIGN_IDENTITY")
        if not identity:
            print("[git-dist] WARNING: MACOS_SIGN_IDENTITY unset — skipping signing")
            return
        entitlements = os.path.join(ROOT, "gcm-entitlements.plist")
        for binary in _executables(dest):
            cmd = [
                "codesign", "--deep", "--force", "--verify", "--verbose",
                "--timestamp", "--options", "runtime",
            ]
            # GCM's CoreCLR needs JIT entitlements under the hardened runtime
            # — a re-signed binary without them crashes at startup. Same set
            # GCM's own release signatures carry (dylibs don't take
            # entitlements, only the main executables).
            in_gcm = os.path.basename(os.path.dirname(binary)) == "gcm"
            if in_gcm and not binary.endswith(".dylib"):
                cmd += ["--entitlements", entitlements]
            _run(cmd + ["--sign", identity, binary])
        print("macOS signing complete (notarization happens in CI after packaging)")
    elif platform.system() == "Windows":
        script = os.environ.get("WINDOWS_SIGN_SCRIPT") or os.path.join(ROOT, "sign-windows.ps1")
        if not os.path.exists(script):
            print(f"[git-dist] WARNING: sign script not found ({script}) — skipping signing")
            return
        _run_shell(
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" '
            f'-folderPath "{dest}"'
        )
        print("Windows signing complete")


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def _zip_symlink(zf, abs_path: str, arc: str) -> None:
    """Record a symlink in the zip as a real symlink (not its dereferenced
    target). The Unix create_system + S_IFLNK mode bit is what bsdtar/unzip read
    to recreate it as a symlink on extraction."""
    info = zipfile.ZipInfo(arc)
    info.create_system = 3  # Unix
    info.external_attr = (stat.S_IFLNK | 0o755) << 16
    zf.writestr(info, os.readlink(abs_path))


def package_targz(dest: str, asset_name: str) -> None:
    """Linux archive. tar preserves symlinks and exec bits natively, and the
    consumer side (GNU tar) can't read zips — mirrors 3d-tools' convention of
    zip on Windows, tar.gz elsewhere."""
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    tar_path = os.path.join(ROOT, "dist", f"{asset_name}.tar.gz")
    print(f"Packaging {dest} -> {tar_path}")
    if os.path.exists(tar_path):
        os.remove(tar_path)
    with tarfile.open(tar_path, "w:gz") as tf:
        for entry in sorted(os.listdir(dest)):
            tf.add(os.path.join(dest, entry), arcname=entry)
    digest = _sha256(tar_path)
    with open(tar_path + ".sha256", "w", newline="\n") as f:
        f.write(f"{digest}  {asset_name}.tar.gz\n")
    print(f"sha256 {digest}")


def package(dest: str, asset_name: str) -> None:
    if platform.system() == "Linux":
        package_targz(dest, asset_name)
        return
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    zip_path = os.path.join(ROOT, "dist", f"{asset_name}.zip")
    print(f"Packaging {dest} -> {zip_path}")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(dest):
            # Store symlinks AS symlinks. git installs its ~145 builtins
            # (git-add, git-worktree, ...) as symlinks to `git` when built with
            # NO_INSTALL_HARDLINKS; zipfile.write() would dereference them and
            # write ~145 full copies of the git binary (~500 MB). Recording them
            # as symlink entries keeps the archive ~15 MB and the extracted tree
            # relocatable -- matching the on-disk install (and AP1's git layout).
            for name in list(dirs):
                abs_path = os.path.join(folder, name)
                if os.path.islink(abs_path):
                    _zip_symlink(zf, abs_path, os.path.relpath(abs_path, dest))
                    dirs.remove(name)  # recorded as a symlink; don't descend
            for name in files:
                abs_path = os.path.join(folder, name)
                arc = os.path.relpath(abs_path, dest)
                if os.path.islink(abs_path):
                    _zip_symlink(zf, abs_path, arc)
                else:
                    zf.write(abs_path, arc)
    digest = _sha256(zip_path)
    # newline="\n": keep an LF-only sidecar so `sha256sum -c` works for consumers
    # (text mode would write CRLF on Windows and break filename matching).
    with open(zip_path + ".sha256", "w", newline="\n") as f:
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
    ext = "tar.gz" if platform.system() == "Linux" else "zip"
    files = [
        os.path.join(ROOT, "dist", f"{asset}.{ext}"),
        os.path.join(ROOT, "dist", f"{asset}.{ext}.sha256"),
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
    prefix = "git-linux" if platform.system() == "Linux" else "git-macos"
    return f"{prefix}-{'arm64' if arch == 'arm64' else 'x64'}"


def _executables(dest: str):
    for sub in ("bin", "libexec/git-core"):
        d = os.path.join(dest, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and (os.access(p, os.X_OK) or name.endswith(".dylib")):
                yield p
    # The GCM tree needs an explicit allowlist: its ~200 .NET assemblies
    # (.dll) carry the executable bit but are PE files codesign rejects.
    # Mach-O files there are the two apphosts and the native .dylibs.
    gcm = os.path.join(dest, "gcm")
    if os.path.isdir(gcm):
        for name in os.listdir(gcm):
            if name.endswith(".dylib") or name in ("git-credential-manager", "createdump"):
                yield os.path.join(gcm, name)


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


def _sdk_bash(gitsdk: str, script: str) -> int:
    """Run a script in the SDK's MSYS2/MINGW64 login shell, synchronously.

    git-bash.exe is a detached launcher (it returns before the command finishes
    and sends output to a separate window), so it can't be driven from a build
    script. bash.exe -lc runs in-process under MSYSTEM=MINGW64 and returns the
    real exit code. MSYS paths (/usr/src/...) resolve under the SDK root.

    The MinGW toolchain needs a writable temp dir. The SDK's /etc/profile keeps
    TMP/TEMP from the Windows environment but does not default them, so in a
    headless/CI shell (where Windows provides none) they stay empty and the
    tools fall back to C:\\Windows and fail. Export them *inside* the script
    (i.e. after profile has run) so they point at a writable directory. Child
    output is streamed through this process so it lands in our logs.
    """
    import subprocess
    bash = os.path.join(gitsdk, "usr", "bin", "bash.exe")
    tmp = os.path.join(ROOT, "build-temp", "tmp")
    os.makedirs(tmp, exist_ok=True)
    tmp_msys = tmp.replace("\\", "/")
    script = f'export TMP="{tmp_msys}" TEMP="{tmp_msys}" TMPDIR="{tmp_msys}"; {script}'
    env = os.environ.copy()
    env["MSYSTEM"] = "MINGW64"
    env["CHERE_INVOKING"] = "1"
    # A build shell is non-interactive; scripts like MinGit's release.sh drive
    # the editor themselves (`-c core.editor=echo`) to read paths back. An
    # inherited GIT_EDITOR/EDITOR/VISUAL (e.g. GIT_EDITOR=true in CI) overrides
    # that and makes them return nothing, so drop them.
    for _var in ("GIT_EDITOR", "EDITOR", "VISUAL"):
        env.pop(_var, None)
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [bash, "-lc", script], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, **kwargs,
    )
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
    return proc.returncode


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
    # Windows signing config -> env (an explicit env var still wins). sign()
    # reads these; sign_script defaults to ./sign-windows.ps1 when unset.
    for opt, var in (("sign_script", "WINDOWS_SIGN_SCRIPT"),
                     ("sign_thumbprint", "WINDOWS_SIGN_THUMBPRINT")):
        if config.has_option("windows", opt):
            os.environ.setdefault(var, config["windows"][opt])
    gitlfs = resolve_path(config, "gitlfs", "GIT_LFS_PATH", required=True)
    arch = args.arch or ("arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64")
    asset = asset_name_for(arch)
    dest = fresh_dest(asset)

    if platform.system() == "Windows":
        gitsdk = resolve_path(config, "gitsdk", "GIT_SDK_PATH", required=True)
        build_git_windows(gitsdk, dest)
        build_gitlfs(gitlfs, dest, goos="windows", goarch="amd64")
        bundle_less(gitsdk, dest)
    elif platform.system() == "Darwin":
        git_source = resolve_path(config, "gitsource", "GIT_SOURCE_PATH", required=True)
        build_git_macos(git_source, dest, arch)
        build_gitlfs(gitlfs, dest, goos="darwin", goarch="amd64" if arch == "x86_64" else "arm64")
        bundle_gcm(dest, arch)
    elif platform.system() == "Linux":
        git_source = resolve_path(config, "gitsource", "GIT_SOURCE_PATH", required=True)
        build_git_linux(git_source, dest)
        build_gitlfs(gitlfs, dest, goos="linux", goarch="amd64" if arch == "x86_64" else "arm64")
        bundle_gcm(dest, arch)
    else:
        raise SystemExit("[git-dist] unsupported platform (Windows, macOS, and Linux only)")

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
