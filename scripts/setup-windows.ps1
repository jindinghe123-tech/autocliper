# AutoStudio for Windows: checks everything it needs, installs whatever is missing, then starts the app.
# Started by AutoStudio.bat (double-click). Arguments are passed through to `python -m autostudio`.
#
# Nothing is installed system-wide and no admin rights are needed: uv, a private Python 3.12 and all
# packages live in this folder (.runtime\ and .venv\), so deleting the folder removes AutoStudio completely.
# Mirrors (Tsinghua / Aliyun PyPI, npmmirror Python builds) are tried automatically when the official
# sources fail; in China Standard Time they are tried first. Force with AUTOSTUDIO_MIRROR=1 (or 0 to disable).
#
# Saved as UTF-8 *with BOM* so that Windows PowerShell 5.1 reads the Chinese text correctly.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { $Host.UI.RawUI.WindowTitle = 'AutoStudio' } catch {}

$AppArgs = @($args)
$SetupVersion = 1          # bump to force every install to re-run the setup once
$PythonVersion = '3.12'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Runtime = Join-Path $Root '.runtime'
$VenvDir = Join-Path $Root '.venv'
$VPy = Join-Path $VenvDir 'Scripts\python.exe'
$Stamp = Join-Path $VenvDir 'autostudio-setup.txt'
$UvExe = Join-Path $Runtime 'uv\uv.exe'
$Curl = Join-Path $env:SystemRoot 'System32\curl.exe'

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Step([string]$msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail([string]$msg) {
  Write-Host ''
  Write-Host "[X] $msg" -ForegroundColor Red
  Write-Host '    如果问题持续，请把这个窗口的内容截图发到 GitHub Issues：https://github.com/jindinghe123-tech/autocliper/issues' -ForegroundColor DarkGray
  exit 1
}

# ---------------------------------------------------------------------------------------------------
# Already running? Then just open the browser instead of failing with "address already in use".

function Get-Local([string]$url) {
  $req = [Net.HttpWebRequest]::Create($url)
  $req.Proxy = $null
  $req.Timeout = 2000
  $resp = $req.GetResponse()
  try { return (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } finally { $resp.Close() }
}

function Test-PortOpen([int]$port) {
  $c = New-Object Net.Sockets.TcpClient
  try { return $c.ConnectAsync('127.0.0.1', $port).Wait(400) -and $c.Connected } catch { return $false } finally { $c.Close() }
}

if ($AppArgs.Count -eq 0) {
  foreach ($p in 8765..8769) {
    if (-not (Test-PortOpen $p)) { continue }
    $isUs = $false
    try { $isUs = (Get-Local "http://127.0.0.1:$p/") -match 'AutoStudio' } catch {}
    if ($isUs) {
      Say "AutoStudio 已经在运行，正在打开 http://127.0.0.1:$p" 'Green'
      Start-Process "http://127.0.0.1:$p"
      exit 0
    }
  }
}

# ---------------------------------------------------------------------------------------------------
# Fast path: everything was installed before and nothing changed since.

$Fingerprint = "v$SetupVersion " + (Get-FileHash -LiteralPath (Join-Path $Root 'pyproject.toml') -Algorithm SHA256).Hash

function Test-VenvPython {
  if (-not (Test-Path -LiteralPath $VPy)) { return $false }
  try {
    & $VPy -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
  } catch { return $false }
}

function Start-App {
  # Undo the download proxy settings so the app itself runs with the user's normal environment.
  foreach ($k in $script:SavedEnv.Keys) { [Environment]::SetEnvironmentVariable($k, $script:SavedEnv[$k], 'Process') }
  $port = 8765
  $extra = @()
  if ($AppArgs.Count -eq 0) {
    while ($port -lt 8780 -and (Test-PortOpen $port)) { $port++ }
    if ($port -ne 8765) {
      Say "端口 8765 被其他程序占用，改用 $port" 'Yellow'
      $extra = @('--port', "$port")
    }
  }
  Write-Host ''
  Say '  AutoStudio 正在启动，浏览器会自动打开。' 'Green'
  Say '  使用期间请保持这个窗口打开；关闭窗口即可退出。' 'DarkGray'
  & $VPy -m autostudio @extra @AppArgs
  exit $LASTEXITCODE
}

$script:SavedEnv = @{}
$stampOk = (Test-Path -LiteralPath $Stamp) -and ((Get-Content -LiteralPath $Stamp -Raw -ErrorAction SilentlyContinue) -eq $Fingerprint)
if ($stampOk -and (Test-VenvPython)) { Start-App }

# ---------------------------------------------------------------------------------------------------
# Setup

Write-Host ''
if (Test-VenvPython) {
  Say '  AutoStudio 有更新，正在同步依赖…' 'Magenta'
} else {
  Say '  AutoStudio 首次运行：正在自动安装所需组件' 'Magenta'
  Say '  全部装在本文件夹里，不需要管理员权限，不会改动系统。'
  Say '  大约需要 3-10 分钟（视网速而定），以后双击就能秒开。'
}

# Download through the system proxy (Clash, v2rayN …) when one is configured; uv and curl only read env vars.
foreach ($k in 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY') { $script:SavedEnv[$k] = [Environment]::GetEnvironmentVariable($k, 'Process') }
if (-not ($env:HTTPS_PROXY -or $env:https_proxy -or $env:ALL_PROXY -or $env:all_proxy)) {
  try {
    $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    if ($ie.ProxyEnable -eq 1 -and $ie.ProxyServer) {
      $p = [string]$ie.ProxyServer
      if ($p -match '=') {
        $m = [regex]::Match($p, '(?:^|;)https=([^;]+)')
        if (-not $m.Success) { $m = [regex]::Match($p, '(?:^|;)http=([^;]+)') }
        $p = if ($m.Success) { $m.Groups[1].Value } else { '' }
      }
      if ($p) {
        if ($p -notmatch '://') { $p = "http://$p" }
        $env:HTTP_PROXY = $p; $env:HTTPS_PROXY = $p; $env:NO_PROXY = 'localhost,127.0.0.1,::1'
        Say "  检测到系统代理 $p，下载将通过代理进行。" 'DarkGray'
      }
    }
  } catch {}
}

$PreferMirror = $false
if ($env:AUTOSTUDIO_MIRROR) { $PreferMirror = $env:AUTOSTUDIO_MIRROR -notin @('0', 'off', 'false', 'no') }
else { try { $PreferMirror = (Get-TimeZone).Id -eq 'China Standard Time' } catch {} }

$Official = @{
  name = '官方源'
  uv = @{ json = 'https://pypi.org/pypi/uv/json' }
  index = 'https://pypi.org/simple'
  python = $null
}
$Tuna = @{
  name = '国内镜像 1'
  uv = @{ simple = 'https://pypi.tuna.tsinghua.edu.cn/simple/uv/' }
  index = 'https://pypi.tuna.tsinghua.edu.cn/simple'
  python = 'https://registry.npmmirror.com/-/binary/python-build-standalone'
}
$Aliyun = @{
  name = '国内镜像 2'
  uv = @{ simple = 'https://mirrors.aliyun.com/pypi/simple/uv/' }
  index = 'https://mirrors.aliyun.com/pypi/simple/'
  python = 'https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone'
}
$Sources = if ($PreferMirror) { @($Tuna, $Aliyun, $Official) } else { @($Official, $Tuna, $Aliyun) }

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$WheelPlat = switch ($arch) { 'AMD64' { 'win_amd64' } 'ARM64' { 'win_arm64' } default { $null } }
if (-not $WheelPlat) { Fail "AutoStudio 需要 64 位 Windows（当前：$arch）。" }

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$env:UV_CACHE_DIR = Join-Path $Runtime 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Runtime 'python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_SYSTEM_CERTS = '1'          # trust the Windows certificate store (antivirus / corporate HTTPS scanning)
$env:UV_HTTP_TIMEOUT = '120'

function Get-File([string]$url, [string]$dest) {
  $tmp = "$dest.part"
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
  $ok = $false
  if (Test-Path -LiteralPath $Curl) {
    & $Curl -fL --retry 2 --connect-timeout 20 --progress-bar -o $tmp $url
    $ok = $LASTEXITCODE -eq 0
  }
  if (-not $ok) {   # old Windows without curl, or curl failed (e.g. certificate revocation check behind a proxy)
    $wc = New-Object Net.WebClient
    $wc.DownloadFile($url, $tmp)
  }
  Move-Item -LiteralPath $tmp -Destination $dest -Force
}

function Find-UvWheel($src) {
  if ($src.json) {
    $d = Invoke-RestMethod -UseBasicParsing -TimeoutSec 30 -Uri $src.json
    $f = $d.urls | Where-Object { $_.filename -like "*-py3-none-$WheelPlat.whl" } | Select-Object -First 1
    if (-not $f) { throw "PyPI 上没有 $WheelPlat 版本的 uv" }
    return @{ url = $f.url; sha256 = $f.digests.sha256 }
  }
  $html = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $src.simple).Content
  $best = $null; $bestV = $null
  foreach ($m in [regex]::Matches($html, 'href="([^"#]*?uv-(\d+\.\d+\.\d+)-py3-none-' + $WheelPlat + '\.whl)#sha256=([0-9a-f]{64})')) {
    $v = [version]$m.Groups[2].Value
    if (-not $bestV -or $v -gt $bestV) { $bestV = $v; $best = $m }
  }
  if (-not $best) { throw '镜像上没有找到 uv' }
  return @{ url = ([Uri]::new([Uri]$src.simple, $best.Groups[1].Value)).AbsoluteUri; sha256 = $best.Groups[3].Value }
}

function Install-Uv {
  if (Test-Path -LiteralPath $UvExe) {
    try { & $UvExe --version 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { return } } catch {}
  }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  foreach ($src in $Sources) {
    try {
      Say "  下载 uv（Python 环境管理器，来源：$($src.name)）…"
      $w = Find-UvWheel $src.uv
      $whl = Join-Path $Runtime 'uv.whl'
      Get-File $w.url $whl
      if ((Get-FileHash -LiteralPath $whl -Algorithm SHA256).Hash -ne $w.sha256.ToUpper()) { throw '文件校验失败' }
      $zip = [IO.Compression.ZipFile]::OpenRead($whl)
      try {
        $entry = $zip.Entries | Where-Object { $_.FullName -like '*.data/scripts/uv.exe' } | Select-Object -First 1
        if (-not $entry) { throw '安装包里没有 uv.exe' }
        New-Item -ItemType Directory -Force -Path (Split-Path $UvExe) | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $UvExe, $true)
      } finally { $zip.Dispose(); Remove-Item -LiteralPath $whl -Force -ErrorAction SilentlyContinue }
      & $UvExe --version | Out-Host
      return
    } catch {
      Say "  $($src.name) 不可用：$($_.Exception.Message)" 'Yellow'
    }
  }
  Fail '无法下载 uv。请检查网络连接（或开启代理）后重新双击 AutoStudio.bat。'
}

function Invoke-Uv([string[]]$uvArgs) {
  & $UvExe @uvArgs | Out-Host
  return $LASTEXITCODE -eq 0
}

# 1. uv ----------------------------------------------------------------------------------------------
Step '1/4 准备安装工具'
Install-Uv

# 2. Python ------------------------------------------------------------------------------------------
Step "2/4 准备 Python 运行环境"
if (Test-VenvPython) {
  Say "  已有运行环境：$(& $VPy --version 2>&1)" 'DarkGray'
} else {
  if (Test-Path -LiteralPath $VenvDir) {
    Say '  旧的运行环境已损坏（文件夹可能被移动过），重新创建…' 'Yellow'
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
  }
  $made = $false
  foreach ($src in $Sources) {
    $env:UV_PYTHON_INSTALL_MIRROR = $src.python
    Say "  下载 Python $PythonVersion（来源：$($src.name)）…"
    if (Invoke-Uv @('venv', $VenvDir, '--python', $PythonVersion, '--no-project')) { $made = $true; break }
    Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (-not $made) { Fail '无法下载 Python。请检查网络连接（或开启代理）后重新双击 AutoStudio.bat。' }
}

# 3. Packages ----------------------------------------------------------------------------------------
Step '3/4 安装依赖（Web 服务、ffmpeg、语音识别、AI 配音、视频下载、发布…）'
function Install-Packages([string]$index, [string[]]$extras) {
  $a = @('pip', 'install', '--python', $VPy, '--default-index', $index, '-r', 'pyproject.toml')
  foreach ($e in $extras) { $a += @('--extra', $e) }
  return Invoke-Uv $a
}
$installed = $false
foreach ($src in $Sources) {
  Say "  来源：$($src.name)"
  if (Install-Packages $src.index @('all')) { $installed = $true; break }
}
if (-not $installed) {
  # The full set failed everywhere: install the core alone, then each optional feature on its own,
  # so one feature without a wheel for this machine doesn't block the whole app.
  Say '  完整安装失败，改为逐项安装…' 'Yellow'
  foreach ($src in $Sources) {
    if (-not (Install-Packages $src.index @())) { continue }
    $installed = $true
    foreach ($extra in 'asr', 'tts', 'download', 'publish') {
      if (-not (Install-Packages $src.index @($extra))) { Say "  可选功能 $extra 安装失败，已跳过" 'Yellow' }
    }
    break
  }
}
if (-not $installed) { Fail '依赖安装失败。请检查网络连接（或开启代理）后重新双击 AutoStudio.bat。' }

# 4. Check -------------------------------------------------------------------------------------------
Step '4/4 检查环境'
& $VPy (Join-Path $Root 'scripts\doctor.py')
if ($LASTEXITCODE -ne 0) { Fail '环境检查没有通过（见上方标 [X] 的项目）。' }

Set-Content -LiteralPath $Stamp -Value $Fingerprint -NoNewline -Encoding ASCII
Write-Host ''
Say '  安装完成！以后直接双击 AutoStudio.bat 即可启动。' 'Green'
Start-App
