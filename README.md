# git-dist

Builds a **portable Git distribution** for Windows and macOS, bundled with
[Anchorpoint's `git-lfs` fork](https://github.com/Anchorpoint-Software/git-lfs),
and publishes the per-OS archives as GitHub **release assets**.

Anchorpoint 2 (and AP1 before it) ships its own Git rather than relying on the
user's system Git. The bundled `git-lfs` is a fork that adds `git lfs fetch
--stdin` (selective include/exclude by path list) — which the app uses to fetch
only the blobs it actually needs (e.g. sparse `download` / `disable`), instead
of fetching every LFS object in the repo. Stock `git-lfs` lacks `--stdin`, so
the fork must travel with the app.

This repo is the **build + publish** side. The consumer side (downloading a
pinned archive at build time and pointing the app at it) lives in the app repo.

## What it produces

Per release tag, three signed archives plus checksums, attached to the GitHub
release:

| Asset | Platform |
|-------|----------|
| `git-windows-x64.zip` (+ `.sha256`) | Windows x64 (MinGit) |
| `git-macos-arm64.zip` (+ `.sha256`) | macOS Apple Silicon |
| `git-macos-x64.zip` (+ `.sha256`)   | macOS Intel |

Each archive is a self-contained Git tree with the fork's `git-lfs` in
`libexec/git-core/`, so `git lfs version` reports `Anchorpoint`.

## Layout

```
build.py              # the build: git + fork git-lfs -> dist/<asset>/ -> zip + sha256
config.example.ini    # copy to config.ini; host-specific SDK / source paths
third_party/git-lfs/  # submodule -> Anchorpoint-Software/git-lfs (the fork)
.github/workflows/release.yml  # CI: build + sign + publish on tag
dist/                 # build output (gitignored; published, never committed)
```

## Prerequisites

The fork source is a submodule — clone with it:

```sh
git clone --recurse-submodules https://github.com/Anchorpoint-Software/git-dist
# or, after a plain clone:
git submodule update --init --recursive
```

- **Go** (matching the fork's `go.mod`) — builds `git-lfs`.
- **Windows:** the [Git for Windows SDK](https://github.com/git-for-windows/build-extra)
  (`git-sdk-64`), initialized (`sdk init git`, `sdk init build-extra`) and carrying
  the `anchorpoint` MinGit flavor under `usr/src/build-extra/mingit/`.
- **macOS:** Xcode command-line tools and a checked-out `git/git` source tree at
  the target tag (`gitsource`).
- **Signing (optional locally, required for releases):** a macOS Developer ID
  identity and a Windows code-signing script (see below).

## Build locally

```sh
cp config.example.ini config.ini   # then edit the paths
python build.py --package          # add --nosign to skip signing
python build.py --package --arch x86_64   # macOS: cross-build Intel on Apple Silicon
```

Output lands in `dist/<asset>/` with `dist/<asset>.zip` + `.sha256`.

Paths can also come from env vars (which win over `config.ini`), so CI needs no
file: `GIT_SDK_PATH`, `GIT_LFS_PATH`, `GIT_SOURCE_PATH`.

Signing is driven by env/secrets and is a no-op when unset:
`MACOS_SIGN_IDENTITY` (codesign identity), `WINDOWS_SIGN_SCRIPT` (path to a
PowerShell signing script invoked as `-folderPath <dist>`).

## Releasing

Tags drive releases. Use `v<gitversion>-ap.<n>`:

```
v2.47.0-ap.1
```

where `2.47.0` is the bundled Git version and `ap.1` is the Anchorpoint build
number for that Git version (bump for a fork update or config change at the same
Git version).

Pushing such a tag runs `.github/workflows/release.yml`, which builds all three
targets, signs/notarizes them, and creates the GitHub release with the archives
and `.sha256` files. The consumer (app) pins a specific tag + checksum.

> CI prerequisites left as repo secrets / TODOs: the `anchorpoint` MinGit flavor
> in the Windows SDK, the macOS Developer ID cert + notarization credentials, and
> the Windows signing cert. See the comments in `release.yml`.

## Licensing

The build tooling here is MIT (see `LICENSE`). The **published archives**
redistribute Git (GPLv2), the `git-lfs` fork (MIT), and Git Credential Manager
(MIT) under their own licenses — see the NOTICE in `LICENSE`.
