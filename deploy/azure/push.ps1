# Runs on YOUR laptop. Packs the project, copies it to the Azure VM and runs setup.sh there.
# Started by deploy_azure.bat (asks for the two values). Run again any time to update the live site.
param(
  [string]$VmHost,
  [string]$KeyPath,
  [string]$User = "azureuser"
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")

if (-not $VmHost) { $VmHost = Read-Host "Azure DNS name (like saarthi-meet.centralindia.cloudapp.azure.com)" }
if (-not $KeyPath) { $KeyPath = Read-Host "Path to the downloaded key file (like C:\Users\admin\Downloads\saarthi-key.pem)" }
$VmHost = $VmHost.Trim().Replace("https://", "").Replace("http://", "").TrimEnd("/")
$KeyPath = $KeyPath.Trim().Trim('"')
if (-not (Test-Path $KeyPath)) { throw "Key file not found: $KeyPath" }

# ssh refuses keys that other Windows users can read: keep it readable by you only
icacls $KeyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null

Write-Host "== Packing the project"
$Tgz = Join-Path $env:TEMP "saarthi.tgz"
if (Test-Path $Tgz) { Remove-Item $Tgz -Force }
$Tar = Join-Path $env:SystemRoot "System32\tar.exe"   # Windows' own tar (Git's tar can't read C:\ paths)
& $Tar -czf $Tgz -C $Root --exclude=.venv --exclude=.git --exclude=bin --exclude=android --exclude=__pycache__ `
    --exclude=.pytest_cache --exclude="*.zip" --exclude="data/*.db" --exclude="data/review_link.txt" --exclude=eval/recordings .
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

$Ssh = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
Write-Host "== Copying to $VmHost"
scp @Ssh $Tgz (Join-Path $Root "deploy\azure\setup.sh") "${User}@${VmHost}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "Copy failed. Check the DNS name, the key file, and that port 22 is open on the VM." }

Write-Host "== Installing on the VM (first time ~5-10 minutes)"
ssh @Ssh "${User}@${VmHost}" "sed -i 's/\r$//' /tmp/setup.sh && sudo bash /tmp/setup.sh $VmHost"
if ($LASTEXITCODE -ne 0) { throw "Setup on the VM failed (see the lines above)." }

$Line = ssh @Ssh "${User}@${VmHost}" "sudo cat /opt/saarthi/data/last_deploy.txt"
$Link = ($Line -replace "^LINK ", "").Trim()
Set-Content -Path (Join-Path $Root "data\live_link.txt") -Value $Link -Encoding utf8
Write-Host ""
Write-Host "Done. Opening $Link"
Start-Process $Link
