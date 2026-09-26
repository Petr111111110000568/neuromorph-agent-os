[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256
)
$ErrorActionPreference = 'Stop'
function Assert-Plain([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    if($current.StartsWith('\\')) { throw 'Network paths are not supported by this bootstrap.' }
    while($current) {
        if(Test-Path -LiteralPath $current) {
            if((Get-Item -Force -LiteralPath $current).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point is not permitted.' }
        }
        $parent = [IO.Directory]::GetParent($current)
        if($null -eq $parent) { break }
        $current = $parent.FullName
    }
}
$bundlePath = [IO.Path]::GetFullPath($Bundle)
$destinationPath = [IO.Path]::GetFullPath($Destination)
Assert-Plain $bundlePath
Assert-Plain $destinationPath
if(Test-Path -LiteralPath $destinationPath) { throw 'Destination must not already exist.' }
if(-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($destinationPath)) -PathType Container)) { throw 'Destination parent must exist.' }
$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
Assert-Plain $tempParent
$stage = Join-Path $tempParent ('NeuroMorf-bootstrap-' + [Guid]::NewGuid().ToString('N'))
$createdFiles = [Collections.Generic.List[string]]::new()
$stream = $null
$archive = $null
$madeStage = $false
try {
    # Hold the same file open without write sharing while hashing and using it.
    $stream = [IO.File]::Open($bundlePath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $actual = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
    if($actual -ne $ExpectedSha256) { throw 'Archive differs from the independently supplied SHA-256.' }
    $stream.Position = 0
    Add-Type -AssemblyName System.IO.Compression
    $archive = [IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read,$true)
    if(Test-Path -LiteralPath $stage) { throw 'Bootstrap staging path exists.' }
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    $madeStage = $true
    # The exact archive is trusted by its out-of-band digest before any runtime is extracted.
    foreach($entry in $archive.Entries) {
        if($entry.FullName.StartsWith('python/')) { $name = $entry.FullName.Substring(7) }
        elseif($entry.FullName -eq 'app/scripts/offline_bundle.py') { $name = 'offline_bundle.py' }
        else { continue }
        if($name -notmatch '^[A-Za-z0-9_.-]+$' -or $name -in @('.','..') -or $entry.Length -gt 32MB) { throw 'Unexpected bootstrap member.' }
        $target = Join-Path $stage $name
        $out = [IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $createdFiles.Add($target)
        $inputStream = $entry.Open()
        try { $inputStream.CopyTo($out) } finally { $inputStream.Dispose(); $out.Dispose() }
    }
    $python = Join-Path $stage 'python.exe'
    $installer = Join-Path $stage 'offline_bundle.py'
    if(-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Windows Python and installer are required in this bundle.' }
    & $python -I $installer install $bundlePath $destinationPath --expected-sha256 $ExpectedSha256
    if($LASTEXITCODE -ne 0) { throw 'Verified installer returned an error; application was not launched.' }
    Write-Output 'Installed. Start.cmd launches the application explicitly; no global Python installation was required.'
} finally {
    if($null -ne $archive) { $archive.Dispose() }
    if($null -ne $stream) { $stream.Dispose() }
    if($madeStage) {
        if([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($stage)) -ne $tempParent.TrimEnd('\') -or [IO.Path]::GetFileName($stage) -notmatch '^NeuroMorf-bootstrap-[0-9a-f]{32}$') { throw 'Unexpected cleanup path; retained for inspection.' }
        Assert-Plain $stage
        foreach($file in $createdFiles) { Assert-Plain $file; [IO.File]::Delete($file) }
        [IO.Directory]::Delete($stage,$false)
    }
}
