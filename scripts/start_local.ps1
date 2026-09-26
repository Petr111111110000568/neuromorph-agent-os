[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$RuntimeRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit
)
$ErrorActionPreference = 'Stop'
function Assert-PlainPath([string]$Value) {
    if (-not [IO.Path]::IsPathRooted($Value) -or $Value.Contains('"') -or $Value -match '(^|[\\/])\.\.([\\/]|$)') { throw 'An absolute, nontraversing path is required.' }
    $itemPath = [IO.Path]::GetFullPath($Value)
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -Force -LiteralPath $itemPath
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are not allowed.' }
        }
        $parent = [IO.Directory]::GetParent($itemPath)
        if ($null -eq $parent) { break }
        $itemPath = $parent.FullName
    }
}
Assert-PlainPath $RuntimeRoot
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$python = Join-Path $RuntimeRoot 'venv\Scripts\python.exe'
$service = Join-Path $RuntimeRoot 'service'
$wrapper = Join-Path $PSScriptRoot 'local_windows_service.py'
foreach ($path in @($python, $service, $wrapper)) { Assert-PlainPath $path }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Existing runtime venv is required.' }
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) { throw 'Supervisor source is missing.' }
New-Item -ItemType Directory -Path $service -Force | Out-Null
$receiptPath = Join-Path $service 'receipt.json'
if (Test-Path -LiteralPath $receiptPath) {
    Assert-PlainPath $receiptPath
    if ((Get-Item -LiteralPath $receiptPath).Length -gt 65536) { throw 'Receipt too large.' }
    $old = Get-Content -Raw -Encoding UTF8 -LiteralPath $receiptPath | ConvertFrom-Json
    $running = Get-Process -Id $old.root_pid -ErrorAction SilentlyContinue
    if ($running -and $running.StartTime.ToFileTimeUtc().ToString() -eq $old.root_creation_time.filetime) { throw 'This deployment already has an active supervisor.' }
}
$stdout = Join-Path $service 'supervisor.stdout.log'
$stderr = Join-Path $service 'supervisor.stderr.log'
Assert-PlainPath $stdout
Assert-PlainPath $stderr
$launchId = [Guid]::NewGuid().ToString('N')
$arguments = @('-I', '-X', 'utf8', ('"{0}"' -f $wrapper), '--runtime-root', ('"{0}"' -f $RuntimeRoot), '--source-commit', $SourceCommit, '--launch-id', $launchId)
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $RuntimeRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$launcherCreationTime = $process.StartTime.ToFileTimeUtc().ToString()
$deadline = [DateTime]::UtcNow.AddSeconds(40)
while ([DateTime]::UtcNow -lt $deadline) {
    $process.Refresh()
    if (Test-Path -LiteralPath $receiptPath) {
        Assert-PlainPath $receiptPath
        if ((Get-Item -LiteralPath $receiptPath).Length -gt 65536) { throw 'Receipt too large.' }
        $receipt = Get-Content -Raw -Encoding UTF8 -LiteralPath $receiptPath | ConvertFrom-Json
        if ($receipt.launch_id -eq $launchId -and $receipt.status -eq 'running') {
            $owner = Get-Process -Id $receipt.root_pid -ErrorAction SilentlyContinue
            if (-not $owner -or $owner.StartTime.ToFileTimeUtc().ToString() -ne $receipt.root_creation_time.filetime) { throw 'Supervisor identity no longer matches the ready receipt.' }
            $receipt | Add-Member -NotePropertyName start_process_pid -NotePropertyValue $process.Id
            $receipt | Add-Member -NotePropertyName start_process_creation_filetime -NotePropertyValue $launcherCreationTime
            $receipt | ConvertTo-Json -Depth 8
            return
        }
    }
    if ($process.HasExited) { throw "Launcher exited with code $($process.ExitCode); see service/supervisor.stderr.log and child.log." }
    Start-Sleep -Milliseconds 500
}
throw 'Readiness deadline expired; inspect the scoped receipt and use stop_local.ps1.'
