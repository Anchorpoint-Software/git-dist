# Sign every .exe/.dll in a folder with the Anchorpoint code-signing cert via
# signtool, using the SimplySign cloud key (Certum). Invoked by build.py's
# sign() as:  powershell -File sign-windows.ps1 -folderPath <dist folder>
#
# Prereqs: SimplySign Desktop logged in (virtual card mounted), Windows SDK
# (signtool) installed. The cert is selected by thumbprint; override with
# $env:WINDOWS_SIGN_THUMBPRINT if the cert ever changes.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $folderPath,
    [string] $Thumbprint   = $(if ($env:WINDOWS_SIGN_THUMBPRINT) { $env:WINDOWS_SIGN_THUMBPRINT } else { "F4D5B4774A99A3424B96BB7A5E3631526CA0644A" }),
    [string] $TimestampUrl = "http://time.certum.pl"
)
$ErrorActionPreference = "Stop"

# Locate the newest signtool (x64) from the Windows SDK.
$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) { throw "signtool.exe not found - install the Windows 10/11 SDK." }

# Confirm the signing cert is present and usable (SimplySign logged in).
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Thumbprint -eq $Thumbprint }
if (-not $cert)               { throw "Cert $Thumbprint not in CurrentUser\My - is SimplySign Desktop logged in?" }
if (-not $cert.HasPrivateKey) { throw "Cert $Thumbprint has no usable private key - SimplySign card not mounted?" }
Write-Host "signtool : $signtool"
Write-Host "signing  : $($cert.Subject)"
Write-Host "expires  : $($cert.NotAfter)"

# Collect signable binaries.
$files = @(Get-ChildItem -Path $folderPath -Recurse -Include *.exe, *.dll -File -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName)
if ($files.Count -eq 0) { throw "No .exe/.dll found under $folderPath" }
Write-Host "files    : $($files.Count) to sign + timestamp"

# Sign in batches (one signtool call per batch keeps cloud round-trips down);
# retry a batch a few times since the RFC3161 timestamp server can be flaky.
$batchSize = 40
for ($i = 0; $i -lt $files.Count; $i += $batchSize) {
    $batch = $files[$i .. ([Math]::Min($i + $batchSize - 1, $files.Count - 1))]
    $attempt = 0
    do {
        $attempt++
        & $signtool sign /sha1 $Thumbprint /fd sha256 /tr $TimestampUrl /td sha256 $batch
        $ok = ($LASTEXITCODE -eq 0)
        if (-not $ok) {
            if ($attempt -ge 3) { throw "signtool failed (exit $LASTEXITCODE) after $attempt attempts at index $i" }
            Write-Warning "signtool exit $LASTEXITCODE - retry $attempt/3 in 5s (timestamp server busy?)"
            Start-Sleep -Seconds 5
        }
    } while (-not $ok)
    Write-Host ("  signed {0}/{1}" -f ([Math]::Min($i + $batchSize, $files.Count)), $files.Count)
}

# Sanity-check one signature.
& $signtool verify /pa /all $files[0] | Out-Null
if ($LASTEXITCODE -ne 0) { throw "verification failed on $($files[0])" }
Write-Host "Done: $($files.Count) files signed + timestamped, verified OK."
