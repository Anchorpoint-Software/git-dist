#Requires -Version 5
<#
.SYNOPSIS
  One-command Windows release for git-dist.
.DESCRIPTION
  Verifies the SimplySign code-signing cert is mounted (SimplySign Desktop
  logged in) BEFORE the long build, then runs build.py. Any arguments are
  passed through to build.py; with no arguments it defaults to
  "--package --publish" (build -> bundle less -> sign -> package -> release).
  The SimplySign check is skipped when --nosign is passed.
.EXAMPLE
  .\release.ps1                     # build, sign, and publish the GitHub release
.EXAMPLE
  .\release.ps1 --package --nosign  # local unsigned build, no publish
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] $BuildArgs)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $BuildArgs -or $BuildArgs.Count -eq 0) { $BuildArgs = @("--package", "--publish") }

if ($BuildArgs -notcontains "--nosign") {
    $thumb = if ($env:WINDOWS_SIGN_THUMBPRINT) { $env:WINDOWS_SIGN_THUMBPRINT } `
             else { "F4D5B4774A99A3424B96BB7A5E3631526CA0644A" }
    $cert = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object { $_.Thumbprint -eq $thumb -and $_.HasPrivateKey }
    if (-not $cert) {
        throw ("Code-signing cert $thumb is not available. Log into SimplySign " +
               "Desktop so the virtual card is mounted, then re-run (or pass --nosign).")
    }
    Write-Host "SimplySign ready: $($cert.Subject)  (expires $($cert.NotAfter))"
}

Write-Host "Running: python build.py $($BuildArgs -join ' ')"
& python (Join-Path $root "build.py") @BuildArgs
exit $LASTEXITCODE
