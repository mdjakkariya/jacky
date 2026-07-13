# Install the jack coding CLI on Windows. Usage:
#   irm https://raw.githubusercontent.com/mdjakkariya/jacky/main/install.ps1 | iex
$ErrorActionPreference = 'Stop'
$Repo = 'mdjakkariya/jacky'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\jack'

$Version = $env:JACK_VERSION
if (-not $Version) {
  $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
  $Version = $rel.tag_name -replace '^v', ''
}
$Name = "jack-$Version-windows-x64.zip"
$Url  = "https://github.com/$Repo/releases/download/v$Version/$Name"
$Tmp  = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))

Write-Host "downloading $Name ..."
Invoke-WebRequest "$Url" -OutFile (Join-Path $Tmp $Name)
Invoke-WebRequest "$Url.sha256" -OutFile (Join-Path $Tmp "$Name.sha256")
$want = (Get-Content (Join-Path $Tmp "$Name.sha256")).Split(' ')[0].Trim()
$got  = (Get-FileHash (Join-Path $Tmp $Name) -Algorithm SHA256).Hash.ToLower()
if ($want.ToLower() -ne $got) { throw "checksum mismatch - aborting" }

Expand-Archive (Join-Path $Tmp $Name) -DestinationPath $Tmp -Force
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Move-Item (Join-Path $Tmp 'jack.exe') (Join-Path $InstallDir 'jack.exe') -Force
Write-Host "installed jack $Version -> $InstallDir\jack.exe"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$InstallDir*") {
  [Environment]::SetEnvironmentVariable('Path', "$userPath;$InstallDir", 'User')
  Write-Host "added $InstallDir to your PATH (restart the terminal)."
}
