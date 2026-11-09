[CmdletBinding()]
param(
    [string]$Setup = '',
    [string]$Portable = '',
    [string]$BackendExe = '',
    [string]$MainExe = '',

    [string]$PfxPath = '',
    [string]$Thumbprint = '',
    [string]$PfxPassword = '',
    [string]$TimestampUrl = 'http://timestamp.digicert.com',

    [string]$SignToolPath = '',

    [switch]$CheckOnly,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)

function Resolve-FullPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }
    try {
        return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    }
    catch {
        return $null
    }
}

function Get-Sha256 {
    param([string]$Path)
    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    }
    catch {
        return $null
    }
}

function Get-SignatureInfo {
    param([string]$Path)
    $info = [ordered]@{
        status = 'MISSING'
        signerSubject = $null
        signerThumbprint = $null
        timestampSubject = $null
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $info
    }
    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
        $info.status = $sig.Status.ToString()
        if ($null -ne $sig.SignerCertificate) {
            $info.signerSubject = $sig.SignerCertificate.Subject
            $info.signerThumbprint = $sig.SignerCertificate.Thumbprint
        }
        if ($null -ne $sig.TimeStamperCertificate) {
            $info.timestampSubject = $sig.TimeStamperCertificate.Subject
        }
    }
    catch {
        $info.status = 'ERROR'
    }
    return $info
}

function Find-SignTool {
    param([string]$ExplicitPath)

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $resolved = Resolve-FullPath $ExplicitPath
        if ($resolved -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $resolved
        }
        return $null
    }

    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $roots = @()
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $roots += (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin')
        $roots += (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\8.1\bin')
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $roots += (Join-Path $env:ProgramFiles 'Windows Kits\10\bin')
        $roots += (Join-Path $env:ProgramFiles 'Windows Kits\8.1\bin')
    }

    $found = @()
    foreach ($root in $roots) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            $found += @(Get-ChildItem -LiteralPath $root -Filter 'signtool.exe' -Recurse -ErrorAction SilentlyContinue)
        }
    }
    if ($found.Count -eq 0) {
        return $null
    }

    $x64 = @($found | Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } | Sort-Object FullName -Descending)
    if ($x64.Count -gt 0) {
        return $x64[0].FullName
    }
    return ($found | Sort-Object FullName -Descending | Select-Object -First 1).FullName
}

function Resolve-TargetPath {
    param([string]$Provided, [string]$Default)
    if ([string]::IsNullOrWhiteSpace($Provided)) {
        return $Default
    }
    $resolved = Resolve-FullPath $Provided
    if ($resolved) {
        return $resolved
    }
    return $Provided
}

$defaultSetup = Join-Path $repoRoot 'desktop\release\multiagent-2.0.0-x64-setup.exe'
$defaultPortable = Join-Path $repoRoot 'desktop\release\multiagent-2.0.0-x64.exe'
$defaultBackendExe = Join-Path $repoRoot 'desktop\release\win-unpacked\resources\backend\multiagent-backend\multiagent-backend.exe'
$defaultMainExe = Join-Path $repoRoot 'desktop\release\win-unpacked\MultiAgent.exe'

$targetSpecs = @(
    [ordered]@{ role = 'setup'; path = (Resolve-TargetPath $Setup $defaultSetup) },
    [ordered]@{ role = 'portable'; path = (Resolve-TargetPath $Portable $defaultPortable) },
    [ordered]@{ role = 'backendExe'; path = (Resolve-TargetPath $BackendExe $defaultBackendExe) },
    [ordered]@{ role = 'mainExe'; path = (Resolve-TargetPath $MainExe $defaultMainExe) }
)

$signTool = Find-SignTool $SignToolPath
$issues = @()
$certificateIssues = @()

if ([string]::IsNullOrWhiteSpace($TimestampUrl)) {
    $issues += 'TimestampUrl must not be empty.'
}
else {
    $tsUri = $null
    $tsOk = [Uri]::TryCreate($TimestampUrl, [UriKind]::Absolute, [ref]$tsUri)
    if (-not $tsOk -or ($tsUri.Scheme -ne 'http' -and $tsUri.Scheme -ne 'https')) {
        $issues += "TimestampUrl must be an absolute http/https URL: $TimestampUrl"
    }
}

if ([string]::IsNullOrWhiteSpace($signTool)) {
    $issues += 'signtool.exe was not found. Install the Windows SDK or pass -SignToolPath.'
}

$files = @()
foreach ($spec in $targetSpecs) {
    $path = $spec.path
    $exists = $false
    $isFile = $false
    $size = $null
    $sha256 = $null
    $signature = [ordered]@{
        status = 'MISSING'
        signerSubject = $null
        signerThumbprint = $null
        timestampSubject = $null
    }

    if (-not [string]::IsNullOrWhiteSpace($path)) {
        $exists = Test-Path -LiteralPath $path
        if ($exists) {
            $isFile = Test-Path -LiteralPath $path -PathType Leaf
            if ($isFile) {
                $item = Get-Item -LiteralPath $path
                $size = $item.Length
                $sha256 = Get-Sha256 $path
                $signature = Get-SignatureInfo $path
            }
            else {
                $issues += "Target $($spec.role) is not a file: $path"
            }
        }
        else {
            $issues += "Target $($spec.role) does not exist: $path"
        }
    }
    else {
        $issues += "Target $($spec.role) path is empty."
    }

    $files += [ordered]@{
        role = $spec.role
        path = $path
        exists = $exists
        isFile = $isFile
        size = $size
        sha256 = $sha256
        signature = $signature
    }
}

$certificate = [ordered]@{
    requested = $false
    method = 'none'
    found = $false
    usable = $false
    subject = $null
    thumbprint = $null
    notBefore = $null
    notAfter = $null
    hasPrivateKey = $false
    store = $null
    file = $null
    error = $null
    storeCandidates = 0
}

$pfxProvided = -not [string]::IsNullOrWhiteSpace($PfxPath)
$thumbprintProvided = -not [string]::IsNullOrWhiteSpace($Thumbprint)

if ($pfxProvided -and $thumbprintProvided) {
    $certificate.requested = $true
    $certificate.method = 'conflict'
    $certificate.error = 'Both -PfxPath and -Thumbprint were provided; choose exactly one.'
    $certificateIssues += $certificate.error
}
elseif ($pfxProvided) {
    $certificate.requested = $true
    $certificate.method = 'pfx'
    $certificate.file = Resolve-FullPath $PfxPath
    if (-not $certificate.file -or -not (Test-Path -LiteralPath $certificate.file -PathType Leaf)) {
        $certificate.error = "PFX file does not exist: $PfxPath"
        $certificateIssues += $certificate.error
    }
    else {
        $certObject = $null
        try {
            $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
            $certObject = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
                $certificate.file,
                $PfxPassword,
                $flags
            )
            $certificate.found = $true
            $certificate.subject = $certObject.Subject
            $certificate.thumbprint = $certObject.Thumbprint
            $certificate.notBefore = $certObject.NotBefore.ToUniversalTime().ToString('o')
            $certificate.notAfter = $certObject.NotAfter.ToUniversalTime().ToString('o')
            $certificate.hasPrivateKey = $certObject.HasPrivateKey

            if (-not $certObject.HasPrivateKey) {
                $certificate.error = 'PFX certificate has no private key.'
                $certificateIssues += $certificate.error
            }
            elseif ($certObject.NotAfter.ToUniversalTime() -lt (Get-Date).ToUniversalTime()) {
                $certificate.error = "PFX certificate expired at $($certificate.notAfter)."
                $certificateIssues += $certificate.error
            }
            else {
                $certificate.usable = $true
            }
        }
        catch {
            $certificate.error = "PFX could not be loaded: $($_.Exception.Message)"
            $certificateIssues += $certificate.error
        }
        finally {
            if ($null -ne $certObject) {
                $certObject.Dispose()
            }
        }
    }
}
elseif ($thumbprintProvided) {
    $certificate.requested = $true
    $certificate.method = 'thumbprint'
    $normalizedThumbprint = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($normalizedThumbprint -notmatch '^[0-9A-F]{40}$') {
        $certificate.error = 'Thumbprint must be a 40-character SHA1 hex string.'
        $certificateIssues += $certificate.error
    }
    else {
        $stores = @(
            [ordered]@{ name = 'CurrentUser\My'; path = 'Cert:\CurrentUser\My'; machine = $false },
            [ordered]@{ name = 'LocalMachine\My'; path = 'Cert:\LocalMachine\My'; machine = $true }
        )
        $certObject = $null
        foreach ($store in $stores) {
            $candidate = Get-ChildItem -Path $store.path -ErrorAction SilentlyContinue |
                Where-Object { $_.Thumbprint -eq $normalizedThumbprint } |
                Select-Object -First 1
            if ($null -ne $candidate) {
                $certObject = $candidate
                $certificate.store = $store.name
                break
            }
        }

        if ($null -eq $certObject) {
            $certificate.error = "Certificate thumbprint not found in CurrentUser\My or LocalMachine\My: $normalizedThumbprint"
            $certificateIssues += $certificate.error
        }
        else {
            $certificate.found = $true
            $certificate.subject = $certObject.Subject
            $certificate.thumbprint = $certObject.Thumbprint
            $certificate.notBefore = $certObject.NotBefore.ToUniversalTime().ToString('o')
            $certificate.notAfter = $certObject.NotAfter.ToUniversalTime().ToString('o')
            $certificate.hasPrivateKey = $certObject.HasPrivateKey

            if (-not $certObject.HasPrivateKey) {
                $certificate.error = 'Certificate has no private key in its store.'
                $certificateIssues += $certificate.error
            }
            elseif ($certObject.NotAfter.ToUniversalTime() -lt (Get-Date).ToUniversalTime()) {
                $certificate.error = "Certificate expired at $($certificate.notAfter)."
                $certificateIssues += $certificate.error
            }
            else {
                $certificate.usable = $true
            }
        }
    }
}
else {
    $stores = @('Cert:\CurrentUser\My', 'Cert:\LocalMachine\My')
    $candidates = @()
    foreach ($store in $stores) {
        $candidates += @(
            Get-ChildItem -Path $store -CodeSigningCert -ErrorAction SilentlyContinue |
                Where-Object { $_.HasPrivateKey -and $_.NotAfter.ToUniversalTime() -ge (Get-Date).ToUniversalTime() }
        )
    }
    $certificate.storeCandidates = $candidates.Count
    if ($candidates.Count -eq 0) {
        $certificate.error = 'No code-signing certificate with a private key was found in CurrentUser\My or LocalMachine\My.'
    }
    else {
        $certificate.error = "No certificate selected; $($candidates.Count) usable code-signing certificate(s) exist in the store. Pass -Thumbprint."
    }
    $certificateIssues += $certificate.error
}

$allIssues = @($certificateIssues + $issues)
$ready = ($certificate.usable -and $issues.Count -eq 0)

if (-not $certificate.usable) {
    if ($certificate.method -eq 'none') {
        $reason = 'NOT READY: no certificate'
    }
    else {
        $reason = 'NOT READY: no usable certificate'
    }
}
elseif ($issues.Count -gt 0) {
    $reason = "NOT READY: $($issues[0])"
}
else {
    $reason = 'READY'
}

$result = [ordered]@{
    tool = 'desktop/tools/sign_release.ps1'
    mode = $(if ($CheckOnly) { 'check-only' } else { 'sign' })
    ready = $ready
    exitCode = $null
    reason = $reason
    checkedAt = (Get-Date).ToString('o')
    timestampUrl = $TimestampUrl
    signTool = [ordered]@{
        found = (-not [string]::IsNullOrWhiteSpace($signTool))
        path = $signTool
    }
    certificate = $certificate
    targets = $files
    issues = $allIssues
    notes = @(
        'This script never writes the registry or the certificate store.',
        'PFX files are loaded with EphemeralKeySet so private keys are not persisted.',
        'CheckOnly never signs or modifies target files.',
        'Post-build signing does not change copies already embedded inside setup/portable; sign before packaging for fully signed installs.'
    )
}

$signingFailures = @()

if (-not $CheckOnly) {
    if (-not $certificate.usable) {
        $allIssues = @($allIssues + 'Refusing to sign without a usable certificate.')
        $result.issues = $allIssues
        $result.exitCode = 1
    }
    elseif ($issues.Count -gt 0) {
        $allIssues = @($allIssues + 'Refusing to sign because input validation failed.')
        $result.issues = $allIssues
        $result.exitCode = 1
    }
    else {
        $certArgs = @()
        if ($certificate.method -eq 'pfx') {
            $certArgs += @('/f', $certificate.file)
            if (-not [string]::IsNullOrEmpty($PfxPassword)) {
                $certArgs += @('/p', $PfxPassword)
            }
        }
        elseif ($certificate.method -eq 'thumbprint') {
            $certArgs += @('/sha1', $certificate.thumbprint)
            if ($certificate.store -eq 'LocalMachine\My') {
                $certArgs += '/sm'
            }
        }

        foreach ($file in $files) {
            Write-Output ("Signing {0}: {1}" -f $file.role, $file.path)
            & $signTool sign /fd sha256 /tr $TimestampUrl /td sha256 @certArgs $file.path
            $signExitCode = $LASTEXITCODE
            if ($signExitCode -ne 0) {
                $signingFailures += ("signtool sign failed for {0} with exit code {1}." -f $file.role, $signExitCode)
                break
            }

            $signedItem = Get-Item -LiteralPath $file.path
            $file.size = $signedItem.Length
            $file.sha256 = Get-Sha256 $file.path

            $verified = $false
            $sig = $null
            for ($attempt = 0; $attempt -lt 3; $attempt++) {
                $sig = Get-AuthenticodeSignature -LiteralPath $file.path -ErrorAction Stop
                if ($sig.Status -eq 'Valid') {
                    $verified = $true
                    break
                }
                Start-Sleep -Milliseconds 400
            }

            $file.signature = [ordered]@{
                status = $sig.Status.ToString()
                signerSubject = $(if ($null -ne $sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { $null })
                signerThumbprint = $(if ($null -ne $sig.SignerCertificate) { $sig.SignerCertificate.Thumbprint } else { $null })
                timestampSubject = $(if ($null -ne $sig.TimeStamperCertificate) { $sig.TimeStamperCertificate.Subject } else { $null })
            }

            if (-not $verified) {
                $signingFailures += ("Signature verification failed for {0}: {1}" -f $file.role, $sig.Status)
                break
            }
            Write-Output ("Verified Valid: {0}" -f $file.path)
        }

        if ($signingFailures.Count -eq 0) {
            $result.exitCode = 0
            $result.reason = 'SIGNED: all targets Valid'
        }
        else {
            $result.issues = @($allIssues + $signingFailures)
            $result.exitCode = 1
            $result.reason = 'SIGN FAILED'
        }
    }
}
else {
    if (-not $certificate.usable) {
        $result.exitCode = 2
    }
    elseif ($issues.Count -gt 0) {
        $result.exitCode = 1
    }
    else {
        $result.exitCode = 0
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 12
}
else {
    Write-Output 'SIGNING READINESS'
    Write-Output ('Mode       : {0}' -f $result.mode)
    Write-Output ('SignTool   : {0}' -f $(if ($result.signTool.found) { $result.signTool.path } else { 'NOT FOUND' }))
    if ($certificate.usable) {
        Write-Output ('Certificate: {0} ({1})' -f $certificate.subject, $certificate.thumbprint)
    }
    elseif ($certificate.found) {
        Write-Output ('Certificate: FOUND BUT UNUSABLE - {0}' -f $certificate.error)
    }
    else {
        Write-Output ('Certificate: NOT FOUND - {0}' -f $certificate.error)
    }
    Write-Output ('Timestamp  : {0}' -f $TimestampUrl)
    Write-Output ''

    $table = $files |
        ForEach-Object {
            [pscustomobject]@{
                Role = $_.role
                Exists = $_.exists
                Size = $_.size
                Signature = $_.signature.status
                SHA256 = $_.sha256
                Path = $_.path
            }
        } |
        Format-Table -AutoSize |
        Out-String -Width 260
    Write-Output $table.TrimEnd()

    if ($allIssues.Count -gt 0) {
        Write-Output 'Issues:'
        foreach ($issue in $allIssues) {
            Write-Output ("- {0}" -f $issue)
        }
    }
    Write-Output ''
    Write-Output ('STATUS: {0}' -f $reason)
    Write-Output ('EXIT: {0}' -f $result.exitCode)
    Write-Output ''
    Write-Output 'JSON:'
    Write-Output ($result | ConvertTo-Json -Depth 12)
}

exit $result.exitCode
