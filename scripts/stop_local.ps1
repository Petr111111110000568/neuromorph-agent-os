[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RuntimeRoot)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($RuntimeRoot) -or $RuntimeRoot -match '(^|[\\/])\.\.([\\/]|$)') { throw 'An absolute, nontraversing runtime root is required.' }
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
function Assert-PlainPath([string]$Value) {
    $itemPath = $Value
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            if ((Get-Item -Force -LiteralPath $itemPath).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are not allowed.' }
        }
        $parent = [IO.Directory]::GetParent($itemPath)
        if ($null -eq $parent) { break }
        $itemPath = $parent.FullName
    }
}
$receiptPath = Join-Path $RuntimeRoot 'service\receipt.json'
$stopPath = Join-Path $RuntimeRoot 'service\stop.json'
Assert-PlainPath $receiptPath
Assert-PlainPath $stopPath
if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw 'No deployment receipt.' }
if ((Get-Item -LiteralPath $receiptPath).Length -gt 65536) { throw 'Receipt too large.' }
$receipt = Get-Content -Raw -Encoding UTF8 -LiteralPath $receiptPath | ConvertFrom-Json
if ($receipt.schema_version -ne 1 -or $receipt.nonce -notmatch '^[0-9a-f]{48}$' -or $receipt.root_creation_time.filetime -notmatch '^[0-9]{1,20}$') { throw 'Invalid deployment receipt.' }
$process = Get-Process -Id $receipt.root_pid -ErrorAction SilentlyContinue
if (-not $process) { Write-Output 'Supervisor is already absent; no processes were terminated.'; return }
if ($process.StartTime.ToFileTimeUtc().ToString() -ne $receipt.root_creation_time.filetime) { throw 'PID was reused; refusing the stop request.' }
$payload = @{schema_version=1; nonce=$receipt.nonce; operation='stop'} | ConvertTo-Json -Compress
$temporary = $stopPath + '.tmp'
Assert-PlainPath $temporary
[IO.File]::WriteAllText($temporary, $payload + "`n", [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $stopPath -Force
$deadline = [DateTime]::UtcNow.AddSeconds(20)
while ([DateTime]::UtcNow -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) {
        Get-Content -Raw -Encoding UTF8 -LiteralPath $receiptPath
        return
    }
    Start-Sleep -Milliseconds 250
}
throw 'Supervisor did not acknowledge stop in 20 seconds. No broad or unrelated process termination was attempted.'
