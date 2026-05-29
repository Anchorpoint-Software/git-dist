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
.github/workflows/ci.yml   # compile-checks the fork (no sign/publish)
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

Releases are built and signed **locally** — Windows code-signing uses a
short-lived (~3h) token that can't live in CI. Build each platform on its own
machine and publish to a shared tag with `--publish` (needs the `gh` CLI
authenticated with write access here):

```sh
# Windows, with the signing token in the environment:
python build.py --package --publish

# macOS — Apple Silicon, then Intel (notarytool leaves the bytes unchanged):
python build.py --package --arch arm64  --publish
xcrun notarytool submit dist/git-macos-arm64.zip --keychain-profile <profile> --wait
python build.py --package --arch x86_64 --publish
xcrun notarytool submit dist/git-macos-x64.zip --keychain-profile <profile> --wait
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
