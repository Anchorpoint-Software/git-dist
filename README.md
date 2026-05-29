# git-dist

Builds a portable Git distribution for Windows and macOS, bundled with
[Anchorpoint's `git-lfs` fork](https://github.com/Anchorpoint-Software/git-lfs),
and publishes the per-OS archives as GitHub release assets.

Anchorpoint ships its own Git rather than relying on the user's system Git. The
bundled `git-lfs` is a fork that adds `git lfs fetch --stdin` (selective fetch
by path list), so only the needed blobs are fetched instead of every LFS object
in the repo. Stock `git-lfs` lacks `--stdin`, so the fork ships with Anchorpoint.

## Release assets

Each release tag attaches three signed archives, each with a `.sha256`:

| Asset | Platform |
|-------|----------|
| `git-windows-x64.zip` | Windows x64 (MinGit) |
| `git-macos-arm64.zip` | macOS Apple Silicon |
| `git-macos-x64.zip`   | macOS Intel |

Each is a self-contained Git tree with the fork's `git-lfs` in
`libexec/git-core/`, so `git lfs version` reports `Anchorpoint`.

## Layout

```
build.py             # git + fork git-lfs -> dist/<asset>/ -> zip + .sha256
config.example.ini   # copy to config.ini; host-specific SDK / source paths
third_party/git-lfs/ # submodule -> the git-lfs fork
.github/workflows/release.yml
```

## Build

Clone with submodules (`git clone --recurse-submodules ...`), then:

```sh
cp config.example.ini config.ini   # edit the paths
python build.py --package          # --nosign to skip signing
python build.py --package --arch x86_64   # macOS: cross-build Intel
```

Output: `dist/<asset>/` plus `dist/<asset>.zip` + `.sha256`. Paths may instead
come from `GIT_SDK_PATH` / `GIT_LFS_PATH` / `GIT_SOURCE_PATH` (env wins over
`config.ini`). Signing is env-driven and a no-op when unset
(`MACOS_SIGN_IDENTITY`, `WINDOWS_SIGN_SCRIPT`).

**Prerequisites:** Go (per the fork's `go.mod`); on Windows the Git for Windows
SDK with the `anchorpoint` MinGit flavor; on macOS Xcode command-line tools and
a `git/git` source checkout at the target tag.

## Releasing

Push a tag `v<gitversion>-<build>` (e.g. `v2.47.0-1` — the bundled Git version
plus an Anchorpoint build number). CI builds all targets, signs/notarizes, and
creates the release with the archives + checksums. Consumers pin a tag and
verify the `.sha256`.

> CI needs three things as secrets / build-host setup: a token for the private
> `git-lfs` submodule, the `anchorpoint` MinGit flavor, and the
> signing/notarization credentials — see `release.yml`.

## Licensing

The build tooling is MIT (`LICENSE`). The published archives redistribute Git
(GPLv2), the `git-lfs` fork (MIT), and Git Credential Manager (MIT) under their
own licenses.
