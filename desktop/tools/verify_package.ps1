# ============================================================================
#  verify_package.ps1 - smoke test a packaged desktop build.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File desktop\tools\verify_package.ps1
#    powershell -ExecutionPolicy Bypass -File desktop\tools\verify_package.ps1 -Exe "desktop\release\win-unpacked\<app>.exe"
#
#  What it checks:
#    1. the exe starts and a page is loaded
#    2. the packaged backend comes up on a local port and answers /api/health
#    3. graceful close is given a bounded window to finish
#    4. every remaining app process is force-closed as a tree, then rechecked
#    5. the backend port is released (netstat + an actual loopback bind)
#
#  ASCII only on purpose (Windows PowerShell 5.1 reads .ps1 with the ANSI code
#  page unless the file has a BOM), so the default exe is discovered by glob
#  instead of hard-coding the Chinese product name.
# ============================================================================
[CmdletBinding()]
param(
  [string]$Exe = "",
  [int]$CdpPort = 9225,
  [int]$WaitSeconds = 40,
  [int]$GracefulSeconds = 20,
  [int]$ForcedSettleSeconds = 3
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Exe) {
  $unpacked = Join-Path $repo "desktop\release\win-unpacked"
  $candidate = Get-ChildItem -Path $unpacked -Filter *.exe -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending | Select-Object -First 1
  if (-not $candidate) { throw "no exe found under $unpacked - build first" }
  $Exe = $candidate.FullName
}
if (-not (Test-Path $Exe)) { throw "exe not found: $Exe" }
$Exe = (Resolve-Path -LiteralPath $Exe).Path

$appData = Join-Path $env:APPDATA "multiagent-desktop"
$logFile = Join-Path $appData ".multiagent\logs\desktop-backend.log"

function Format-PidList {
  param([object[]]$Pids)
  $unique = @($Pids | ForEach-Object { [int]$_ } | Sort-Object -Unique)
  if ($unique.Count -eq 0) { return "none" }
  return ($unique -join ",")
}

function Get-ProcessSnapshot {
  return @(Get-CimInstance Win32_Process -ErrorAction Stop)
}

function Get-DescendantProcesses {
  param(
    [object[]]$All,
    [int]$RootPid
  )

  $byParent = @{}
  foreach ($p in $All) {
    $parent = [int]$p.ParentProcessId
    if (-not $byParent.ContainsKey($parent)) {
      $byParent[$parent] = New-Object System.Collections.ArrayList
    }
    [void]$byParent[$parent].Add($p)
  }

  $descendants = New-Object System.Collections.ArrayList
  $seen = @{}
  $queue = New-Object System.Collections.Queue
  $queue.Enqueue($RootPid)

  while ($queue.Count -gt 0) {
    $id = [int]$queue.Dequeue()
    if ($seen.ContainsKey($id)) { continue }
    $seen[$id] = $true
    if (-not $byParent.ContainsKey($id)) { continue }

    foreach ($child in $byParent[$id]) {
      $childId = [int]$child.ProcessId
      if ($childId -eq $RootPid -or $seen.ContainsKey($childId)) { continue }
      [void]$descendants.Add($child)
      $queue.Enqueue($childId)
    }
  }

  return @($descendants)
}

function Test-IsBackendProcess {
  param([object]$ProcessInfo)

  if (-not $ProcessInfo) { return $false }
  $name = [string]$ProcessInfo.Name
  if ($name -ieq "multiagent-backend.exe") { return $true }
  $cmd = [string]$ProcessInfo.CommandLine
  if ($cmd -match '(?i)multiagent-backend\.exe') { return $true }
  return $false
}

function Get-AppTree {
  param(
    [object[]]$All,
    [int]$RootPid
  )

  $descendants = @(Get-DescendantProcesses -All $All -RootPid $RootPid)
  $childrenByParent = @{}
  foreach ($p in $descendants) {
    $id = [int]$p.ProcessId
    $parent = [int]$p.ParentProcessId
    if (-not $childrenByParent.ContainsKey($parent)) {
      $childrenByParent[$parent] = New-Object System.Collections.ArrayList
    }
    [void]$childrenByParent[$parent].Add($p)
  }

  $backendRoots = @{}
  foreach ($p in $descendants) {
    $id = [int]$p.ProcessId
    if (Test-IsBackendProcess -ProcessInfo $p) {
      $backendRoots[$id] = $true
    }
  }

  $backendIds = @{}
  $queue = New-Object System.Collections.Queue
  foreach ($id in $backendRoots.Keys) { $queue.Enqueue([int]$id) }

  while ($queue.Count -gt 0) {
    $id = [int]$queue.Dequeue()
    if ($backendIds.ContainsKey($id)) { continue }
    $backendIds[$id] = $true
    if (-not $childrenByParent.ContainsKey($id)) { continue }
    foreach ($child in $childrenByParent[$id]) {
      $queue.Enqueue([int]$child.ProcessId)
    }
  }

  $backend = @($descendants | Where-Object { $backendIds.ContainsKey([int]$_.ProcessId) })
  $electron = @($descendants | Where-Object { -not $backendIds.ContainsKey([int]$_.ProcessId) })

  return [pscustomobject]@{
    Backend = $backend
    Electron = $electron
    BackendRoots = @($backendRoots.Keys)
    All = $descendants
  }
}

function Get-NetstatListeners {
  param(
    [int]$Port,
    [string]$NetstatText
  )

  $listeners = New-Object System.Collections.ArrayList
  $pattern = '^\s*TCP\s+(\S+)\s+(\S+)\s+LISTENING\s+(\d+)\s*$'
  foreach ($line in ($NetstatText -split "`r?`n")) {
    $m = [regex]::Match($line, $pattern)
    if (-not $m.Success) { continue }

    $local = $m.Groups[1].Value
    $portMatch = [regex]::Match($local, ':(\d+)$')
    if (-not $portMatch.Success) { continue }
    if ([int]$portMatch.Groups[1].Value -ne $Port) { continue }

    [void]$listeners.Add([pscustomobject]@{
      LocalAddress = $local
      OwnerPid = [int]$m.Groups[3].Value
    })
  }

  return @($listeners)
}

function Test-LoopbackBind {
  param([int]$Port)

  if ($Port -le 0) { return $false }
  $listener = $null
  try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    $listener.ExclusiveAddressUse = $true
    $listener.Start()
    return $true
  } catch {
    return $false
  } finally {
    if ($listener) {
      try { $listener.Stop() } catch { }
    }
  }
}

function Get-PortState {
  param([int]$Port)

  if ($Port -le 0) {
    return [pscustomobject]@{
      StillListening = $false
      BindOk = $false
      Pids = @()
      NetstatPid0Only = $false
    }
  }

  $netstatText = ""
  try {
    $netstatText = (& netstat -ano -p tcp 2>$null | Out-String)
  } catch { }

  $listeners = @(Get-NetstatListeners -Port $Port -NetstatText $netstatText)
  $realListeners = @($listeners | Where-Object { $_.OwnerPid -gt 0 })
  $bindOk = Test-LoopbackBind -Port $Port

  # If bind succeeded, a netstat row whose pid already exited is stale. Filter
  # it out before deciding that the port is occupied.
  if ($bindOk) {
    $realListeners = @($realListeners | Where-Object {
      Get-Process -Id $_.OwnerPid -ErrorAction SilentlyContinue
    })
  }

  # A transient PID 0 row is not by itself a live listener. Keep it visible,
  # but require a real owner or a failed bind to call the port still occupied.
  $stillListening = ((-not $bindOk) -or ($realListeners.Count -gt 0))

  return [pscustomobject]@{
    StillListening = $stillListening
    BindOk = $bindOk
    Pids = @($listeners | ForEach-Object { $_.OwnerPid })
    NetstatPid0Only = (($listeners.Count -gt 0) -and ($realListeners.Count -eq 0))
  }
}

function Read-AppendedLog {
  param(
    [string]$Path,
    [long]$Start
  )

  if (-not (Test-Path -LiteralPath $Path)) { return "" }
  $fs = $null
  $reader = $null
  try {
    $fs = [System.IO.File]::Open(
      $Path,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Read,
      [System.IO.FileShare]::ReadWrite
    )
    if ($Start -gt $fs.Length) { $Start = 0 }
    $fs.Seek($Start, [System.IO.SeekOrigin]::Begin) | Out-Null
    $reader = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
    return $reader.ReadToEnd()
  } catch {
    return ""
  } finally {
    if ($reader) { $reader.Dispose() }
    if ($fs) { $fs.Dispose() }
  }
}

function Test-BackendHealth {
  param([int]$Port)

  if ($Port -le 0) { return $false }
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    return ($health.ok -eq $true)
  } catch {
    return $false
  }
}

function Invoke-TaskKill {
  param(
    [int]$TargetPid,
    [switch]$Force,
    [switch]$Tree
  )

  $killArgs = @("/PID", [string]$TargetPid)
  if ($Tree) { $killArgs += "/T" }
  if ($Force) { $killArgs += "/F" }
  & taskkill @killArgs 2>&1 | Out-Null
  return $LASTEXITCODE
}

function Get-LiveTreeSummary {
  param(
    [int]$RootPid,
    [bool]$RootAlive
  )

  $all = @(Get-ProcessSnapshot)
  $tree = Get-AppTree -All $all -RootPid $RootPid
  return [pscustomobject]@{
    Backend = @($tree.Backend)
    Electron = @($tree.Electron)
    All = @($tree.All)
    MainAlive = $RootAlive
  }
}

function Test-CleanState {
  param(
    [object]$TreeSummary,
    [object]$PortState
  )

  return (-not $TreeSummary.MainAlive) -and
    (@($TreeSummary.All).Count -eq 0) -and
    (-not $PortState.StillListening)
}

Write-Host "[verify] exe: $Exe"
$exeSize = (Get-Item -LiteralPath $Exe).Length
Write-Host ("[verify] exe size: {0:N1} MB" -f ($exeSize / 1MB))

$logStart = if (Test-Path -LiteralPath $logFile) { (Get-Item -LiteralPath $logFile).Length } else { 0 }

$env:ELECTRON_RUN_AS_NODE = $null
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $Exe -ArgumentList @("--remote-debugging-port=$CdpPort") -PassThru
Write-Host "[verify] started pid=$($proc.Id)"

$result = [ordered]@{
  exe = $Exe
  exeBytes = $exeSize
  pid = $proc.Id
  windowTitle = $null
  pageUrl = $null
  backendPort = $null
  backendPid = $null
  backendHealth = $false
  gracefulClose = $false
  forcedKill = $false
  closeMethod = "none"
  mainLeftOver = "none"
  backendLeftOver = $null
  electronLeftOver = $null
  portStillListening = $null
  portBindOk = $null
  portListenPids = "none"
  netstatPid0Only = $false
  exitReason = $null
  exitCode = $null
}

$pageUrl = $null
$backendPort = 0
$backendPid = 0
$backendHealth = $false
$lastHealthAttempt = [DateTime]::MinValue

# --- wait for a loaded page and a healthy backend ---------------------------
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
  $proc.Refresh()
  if ($proc.HasExited) { break }

  if (-not $pageUrl) {
    try {
      $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json" -TimeoutSec 2
      $target = $targets | Where-Object {
        $_.type -eq "page" -and $_.url -like "http://127.0.0.1:*"
      } | Select-Object -First 1
      if ($target) {
        $pageUrl = $target.url
        $result.pageUrl = $target.url
        if ($target.title) { $result.windowTitle = $target.title }
        $mainWindow = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
        if ($mainWindow -and $mainWindow.MainWindowTitle) {
          $result.windowTitle = $mainWindow.MainWindowTitle
        }
      }
    } catch { }
  }

  $newLog = Read-AppendedLog -Path $logFile -Start $logStart
  $portMatches = [regex]::Matches($newLog, "Uvicorn running on http://127\.0\.0\.1:(\d+)")
  if ($portMatches.Count -gt 0) {
    $backendPort = [int]$portMatches[$portMatches.Count - 1].Groups[1].Value
  } elseif ($pageUrl) {
    $pagePort = [regex]::Match($pageUrl, '^http://127\.0\.0\.1:(\d+)/')
    if ($pagePort.Success) { $backendPort = [int]$pagePort.Groups[1].Value }
  }

  $pidMatches = [regex]::Matches($newLog, "Started server process \[(\d+)\]")
  if ($pidMatches.Count -gt 0) {
    $backendPid = [int]$pidMatches[$pidMatches.Count - 1].Groups[1].Value
  }

  if ($backendPort -gt 0 -and -not $backendHealth -and
      ((Get-Date) - $lastHealthAttempt).TotalSeconds -ge 1) {
    $lastHealthAttempt = Get-Date
    $backendHealth = Test-BackendHealth -Port $backendPort
  }

  if ($pageUrl -and $backendPort -gt 0 -and $backendHealth) { break }
  Start-Sleep -Milliseconds 400
}

$proc.Refresh()
$mainExitedBeforeClose = $proc.HasExited
$startupReason = $null
if ($mainExitedBeforeClose) {
  $startupReason = "main_exited_early"
} elseif (-not $pageUrl) {
  $startupReason = "page_not_loaded"
} elseif ($backendPort -le 0 -or -not $backendHealth) {
  $startupReason = "backend_not_ready"
}

$result.backendPort = if ($backendPort -gt 0) { $backendPort } else { $null }
$result.backendHealth = $backendHealth

# Resolve the exact backend pid from the process tree if the log was not
# available, and classify backend descendants separately from Electron helpers.
$initialAll = @(Get-ProcessSnapshot)
$initialTree = Get-AppTree -All $initialAll -RootPid $proc.Id
if ($backendPid -le 0 -and $initialTree.BackendRoots.Count -gt 0) {
  $backendPid = [int]($initialTree.BackendRoots | Select-Object -First 1)
}
$result.backendPid = if ($backendPid -gt 0) { [string]$backendPid } else { $null }

# --- graceful close: bounded wait for main, app children and the port -------
$closeRequested = -not $proc.HasExited
$gracefulWaitStart = Get-Date
if ($closeRequested) {
  $closedWindow = $false
  try {
    $proc.Refresh()
    $closedWindow = $proc.CloseMainWindow()
  } catch { }

  if ($closedWindow) {
    $result.closeMethod = "CloseMainWindow"
  } else {
    Invoke-TaskKill -TargetPid $proc.Id | Out-Null
    $result.closeMethod = "taskkill"
  }

  $secondSignal = $false
  $gracefulDeadline = (Get-Date).AddSeconds($GracefulSeconds)
  while ((Get-Date) -lt $gracefulDeadline) {
    $proc.Refresh()
    $mainAlive = -not $proc.HasExited
    $live = Get-LiveTreeSummary -RootPid $proc.Id -RootAlive $mainAlive
    $portState = Get-PortState -Port $backendPort

    if (Test-CleanState -TreeSummary $live -PortState $portState) { break }

    if ((-not $secondSignal) -and $mainAlive -and
        ((Get-Date) - $gracefulWaitStart).TotalSeconds -ge 5) {
      Invoke-TaskKill -TargetPid $proc.Id | Out-Null
      $result.closeMethod = "$($result.closeMethod)+taskkill"
      $secondSignal = $true
    }

    Start-Sleep -Milliseconds 600
  }
}

$proc.Refresh()
$result.gracefulClose = ($closeRequested -and $proc.HasExited)
$result.gracefulWaitSeconds = [math]::Round(((Get-Date) - $gracefulWaitStart).TotalSeconds, 1)

# Let late exits / port release settle before deciding whether force is needed.
Start-Sleep -Seconds 1
$proc.Refresh()
$preForceTree = Get-LiveTreeSummary -RootPid $proc.Id -RootAlive (-not $proc.HasExited)
$preForcePort = Get-PortState -Port $backendPort

$remainingPids = @()
foreach ($p in $preForceTree.Backend) { $remainingPids += [int]$p.ProcessId }
foreach ($p in $preForceTree.Electron) { $remainingPids += [int]$p.ProcessId }
$needsForce = $preForceTree.MainAlive -or ($remainingPids.Count -gt 0)

if ($needsForce) {
  $result.forcedKill = $true
  Write-Host ("[verify] force-closing main={0} app={1}" -f `
    $preForceTree.MainAlive, (Format-PidList -Pids $remainingPids))

  if ($preForceTree.MainAlive) {
    Invoke-TaskKill -TargetPid $proc.Id -Force -Tree | Out-Null
  }
  foreach ($pidToKill in @($remainingPids | Sort-Object -Unique)) {
    Invoke-TaskKill -TargetPid ([int]$pidToKill) -Force -Tree | Out-Null
  }

  # Bounded post-force recheck: do not declare success on the same tick as the
  # kill, and do not leave the final verdict dependent on a single snapshot.
  $settleDeadline = (Get-Date).AddSeconds($ForcedSettleSeconds)
  while ((Get-Date) -lt $settleDeadline) {
    Start-Sleep -Milliseconds 300
    $proc.Refresh()
    $mainAlive = -not $proc.HasExited
    $settleTree = Get-LiveTreeSummary -RootPid $proc.Id -RootAlive $mainAlive
    $settlePort = Get-PortState -Port $backendPort
    if (Test-CleanState -TreeSummary $settleTree -PortState $settlePort) { break }
  }
} elseif ($preForcePort.StillListening) {
  # No app process remains. Give a transient listener a short, bounded chance
  # to disappear without killing an unrelated process.
  $releaseDeadline = (Get-Date).AddSeconds($ForcedSettleSeconds)
  while ((Get-Date) -lt $releaseDeadline) {
    Start-Sleep -Milliseconds 300
    $preForcePort = Get-PortState -Port $backendPort
    if (-not $preForcePort.StillListening) { break }
  }
}

# --- final verdict -----------------------------------------------------------
$proc.Refresh()
$mainAlive = -not $proc.HasExited
$finalAll = @(Get-ProcessSnapshot)
$finalTree = Get-AppTree -All $finalAll -RootPid $proc.Id
$finalPort = Get-PortState -Port $backendPort

$backendLeft = @($finalTree.Backend | ForEach-Object { [int]$_.ProcessId })
$electronLeft = @($finalTree.Electron | ForEach-Object { [int]$_.ProcessId })
$result.mainLeftOver = if ($mainAlive) { [string]$proc.Id } else { "none" }
$result.backendLeftOver = Format-PidList -Pids $backendLeft
$result.electronLeftOver = Format-PidList -Pids $electronLeft
$result.portStillListening = [bool]$finalPort.StillListening
$result.portBindOk = [bool]$finalPort.BindOk
$result.portListenPids = Format-PidList -Pids $finalPort.Pids
$result.netstatPid0Only = [bool]$finalPort.NetstatPid0Only

$processResidual = $mainAlive -or ($backendLeft.Count -gt 0) -or ($electronLeft.Count -gt 0)
$exitReason = $startupReason
$exitCode = 0

if (-not $exitReason) {
  if ($processResidual) {
    $exitReason = "residual_processes"
    $exitCode = 3
  } elseif ($finalPort.StillListening) {
    $exitReason = "port_still_bound"
    $exitCode = 3
  } elseif ($result.forcedKill) {
    $exitReason = "clean_after_forced_kill"
    $exitCode = 0
  } else {
    $exitReason = "clean_graceful_close"
    $exitCode = 0
  }
} else {
  switch ($startupReason) {
    "main_exited_early" { $exitCode = 1 }
    "page_not_loaded" { $exitCode = 2 }
    "backend_not_ready" { $exitCode = 4 }
    default { $exitCode = 1 }
  }
  if ($processResidual) {
    $exitReason = "$startupReason+residual_processes"
    $exitCode = 3
  } elseif ($finalPort.StillListening) {
    $exitReason = "$startupReason+port_still_bound"
    $exitCode = 3
  }
}

$result.exitReason = $exitReason
$result.exitCode = $exitCode
$result | ConvertTo-Json
exit $exitCode
