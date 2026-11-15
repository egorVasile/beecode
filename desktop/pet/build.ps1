$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'build') | Out-Null
$assets = @('bee-parts.png','transition.png','typing.png','lie.png','fly.png','expressions-clean.png','lie-work.png','yellow-bee-parts.png','yellow-transition.png','yellow-typing.png','yellow-lie.png','yellow-fly.png','yellow-lie-work.png','aligned-status-a.png','aligned-status-b.png','aligned-status-c.png','aligned-yellow-status-a.png','aligned-yellow-status-b.png','aligned-yellow-status-c.png')
$assetArgs = @()
foreach ($name in $assets) { $assetArgs += @('--add-data', ((Join-Path $PSScriptRoot $name) + ';.')) }
python -m PyInstaller --noconfirm --onefile --windowed --name BeeCodePet @assetArgs --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $PSScriptRoot 'build') --specpath (Join-Path $PSScriptRoot 'build') (Join-Path $PSScriptRoot 'pet.py')
if ($LASTEXITCODE -ne 0) { throw "Pet build failed: $LASTEXITCODE" }
