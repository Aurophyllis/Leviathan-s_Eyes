param(
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$outputPath = [System.IO.Path]::GetFullPath($Output)
$dist = Join-Path $outputPath "dist"
$work = Join-Path $outputPath "work"
$spec = Join-Path $outputPath "spec"
$buildArgs = @(
    "--noconfirm", "--onedir", "--console", "--name", "LeviathanEyes",
    "--paths", $repo, "--distpath", $dist, "--workpath", $work, "--specpath", $spec,
    (Join-Path $repo "windows_entry.py")
)
& $Python -m PyInstaller @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
$bundle = Join-Path $dist "LeviathanEyes"
Copy-Item -LiteralPath (Join-Path $repo "docs/live-vision.md") -Destination (Join-Path $bundle "START-HERE.md")
Compress-Archive -LiteralPath $bundle -DestinationPath (Join-Path $outputPath "LeviathanEyes-win-x64.zip") -Force
