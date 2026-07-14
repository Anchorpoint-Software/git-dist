# git-dist

Builds a portable Git distribution for Windows, macOS, and Linux, bundled with
[Anchorpoint's `git-lfs` fork](https://github.com/Anchorpoint-Software/git-lfs)
and [Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager),
and publishes the per-OS archives as GitHub release assets.

Anchorpoint ships its own Git rather than relying on the user's system Git. The
bundled `git-lfs` is a fork that adds `git lfs fetch --stdin` (selective fetch
by path list), so only the needed blobs are fetched instead of every LFS object
in the repo. Stock `git-lfs` lacks `--stdin`, so the fork ships with Anchorpoint.

## Release assets

Each release tag attaches these archives (signed on Windows/macOS), each with a
`.sha256`:

| Asset | Platform |
|-------|----------|
| `git-windows-x64.zip` | Windows x64 (MinGit) |
| `git-macos-arm64.zip` | macOS Apple Silicon |
| `git-macos-x64.zip`   | macOS Intel |
| `git-linux-x64.tar.gz` | Linux x64 |

Each is a self-contained Git tree with the fork's `git-lfs` in
`libexec/git-core/`, so `git lfs version` reports `Anchorpoint`.

Credential handling matches across platforms: MinGit ships GCM on Windows; on
macOS/Linux the pinned official GCM release (`GCM_VERSION`/`GCM_SHA256` in
`build.py`) is bundled under `gcm/` and wired through a baked `etc/gitconfig`
(`credential.helper = manager`, plus the lfs filter block that MinGit's
gitconfig already carries). On Linux the store defaults to the freedesktop
Secret Service (`credential.credentialStore = secretservice`, i.e. the
GNOME/KDE keyrings); override with `GCM_CREDENTIAL_STORE` on headless setups.

## Layout

```
build.py             # git + fork git-lfs + GCM -> dist/<asset>/ -> zip + .sha256
release.ps1          # Windows one-command release (checks SimplySign, runs build.py)
sign-windows.ps1     # signs the dist's .exe/.dll via signtool (SimplySign cert)
gcm-entitlements.plist   # hardened-runtime JIT entitlements for re-signing GCM (macOS)
config.example.ini   # copy to config.ini; host-specific paths + signing
third_party/git-lfs/ # submodule -> the git-lfs fork
.github/workflows/ci.yml   # compile-checks the fork (no sign/publish)
```

## Build

Clone with submodules (`git clone --recurse-submodules ...`), then:

```sh
cp config.example.ini config.ini   # set the SDK path (Windows) / source path (macOS)
python build.py --package          # add --nosign to skip signing
python build.py --package --arch x86_64   # macOS: cross-build Intel
```

Output: `dist/<asset>/` plus `dist/<asset>.zip` + `.sha256`. Paths may instead
come from `GIT_SDK_PATH` / `GIT_LFS_PATH` / `GIT_SOURCE_PATH` (env wins over
`config.ini`). On Windows the SDK's `git`/`build-extra` sources are
auto-initialized on the first build, the `less` pager is bundled, and signing
defaults to `./sign-windows.ps1`.

### First-time host setup

One-time per machine (not per release):

- **Windows** — install [Go](https://go.dev/dl/), the Git for Windows SDK
  (`git-sdk-64`), the Windows SDK (for `signtool`), and SimplySign Desktop. Copy
  `config.example.ini` to `config.ini` and set `[gitsdk].path`; the Git and
  build-extra sources are fetched automatically on the first build.
- **macOS** — install Go and Xcode command-line tools, and check out `git/git`
  at the target tag; set `[gitsource].path`.
- **Linux** — install Go plus `gcc`/`make`, `libssl-dev`, `libcurl4-openssl-dev`,
  `zlib1g-dev`, `libexpat1-dev`, and `gettext`; check out `git/git` at the
  target tag and set `[gitsource].path`.

Signing is config/env-driven and a no-op when unset: macOS uses
`MACOS_SIGN_IDENTITY`; Windows uses `sign-windows.ps1` with the
`[windows].sign_thumbprint` cert (overridable via `WINDOWS_SIGN_SCRIPT` /
`WINDOWS_SIGN_THUMBPRINT`).

## Releasing

Releases are built and signed **locally** — Windows code-signing uses a
short-lived (~3h) token that can't live in CI. Build each platform on its own
machine and publish to a shared tag with `--publish` (needs the `gh` CLI
authenticated with write access here):

```sh
# Windows — log into SimplySign Desktop first, then one command does it all
# (verifies the cert, builds, signs, packages, and publishes the release):
.\release.ps1                      # == python build.py --package --publish

# macOS — Apple Silicon, then Intel (notarytool leaves the bytes unchanged):
python build.py --package --arch arm64  --publish
xcrun notarytool submit dist/git-macos-arm64.zip --keychain-profile <profile> --wait
python build.py --package --arch x86_64 --publish
xcrun notarytool submit dist/git-macos-x64.zip --keychain-profile <profile> --wait

# Linux — unsigned; builds and uploads into the same tag:
python build.py --package --publish
```

`--publish` auto-derives the tag `v<gitversion>.anchorpoint.<n>` (GfW-style, like
Git for Windows' `.windows.N`) from the Git version it just built: it reuses the
highest existing `…anchorpoint.<n>` for that Git version, or starts at `.1`. So
the first machine creates `…anchorpoint.1` and the others upload into the *same*
release (clobbering a prior upload of the same name) — all three archives land on
one tag. To re-cut the same Git version (e.g. a `git-lfs` fork or gitconfig
change), run the first machine with `--bump` (→ `…anchorpoint.2`) and the rest
plain `--publish`; override entirely with `--tag v2.47.0.anchorpoint.3`. Record
the exact fork commit in the release notes; consumers pin a tag and verify the
`.sha256`.

CI (`.github/workflows/ci.yml`) only compile-checks the `git-lfs` fork across
targets — it does not sign or publish. It needs `SUBMODULE_TOKEN` (read access
to the private fork) to clone the submodule.

## Licensing

The build tooling is MIT (`LICENSE`). The published archives redistribute Git
(GPLv2), the `git-lfs` fork (MIT), and Git Credential Manager (MIT) under their
own licenses.
