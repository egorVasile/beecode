$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$releaseRoot = Join-Path $sourceRoot 'release-rust-clean-5.0.2'
$payloadRoot = Join-Path $releaseRoot 'payload'
if (Test-Path -LiteralPath $releaseRoot) { throw "Release directory already exists: $releaseRoot" }
New-Item -ItemType Directory -Path $payloadRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'target\release\beecode-desktop-rust.exe') -Destination $payloadRoot
$uiDest = Join-Path $payloadRoot 'resources\ui'
$agentDest = Join-Path $payloadRoot 'resources\agent-src\beeagent'
& robocopy (Join-Path $sourceRoot 'ui') $uiDest /E /XD '__pycache__' /XF '*.pyc' 'keys.json' 'beeagent.json' '.env' /NJH /NJS /NDL /NFL /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "UI copy failed: $LASTEXITCODE" }
& robocopy (Join-Path $sourceRoot 'agent-src\beeagent') $agentDest /E /XD '__pycache__' /XF '*.pyc' 'keys.json' 'beeagent.json' '.env' /NJH /NJS /NDL /NFL /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Agent copy failed: $LASTEXITCODE" }
foreach ($name in @('pyproject.toml','README.md','LICENSE')) {
  Copy-Item -LiteralPath (Join-Path $sourceRoot "agent-src\$name") -Destination (Join-Path $payloadRoot 'resources\agent-src')
}
$nsis = Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis\nsis-3.0.4.1\makensis.exe'
& $nsis "/DPAYLOAD=$payloadRoot" "/DOUTPUT=$releaseRoot" (Join-Path $PSScriptRoot 'installer.nsi')
if ($LASTEXITCODE -ne 0) { throw "Installer creation failed: $LASTEXITCODE" }
Get-Item -LiteralPath (Join-Path $releaseRoot 'BeeCode Rust Setup 5.0.2.exe') | Select-Object FullName,Length
