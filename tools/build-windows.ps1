# Membuat ZIP portabel Windows x64. Memerlukan Python 3.10+ dengan PyInstaller
# dan Cargo pada komputer pembuat paket. Pengguna ZIP tidak perlu memasangnya.
[CmdletBinding()]
param(
    [string] $OutputDir = (Join-Path $PSScriptRoot '..\dist'),
    [string] $Python = 'python'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$output = [IO.Directory]::CreateDirectory($OutputDir).FullName
$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$work = Join-Path $tempParent ('msshi-doctor-build-' + [Guid]::NewGuid().ToString('N'))
[void] [IO.Directory]::CreateDirectory($work)

try {
    & $Python -I -c "import platform, sys; sys.exit(0 if sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64') and sys.maxsize > 2**32 else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Gunakan Python Windows x64 untuk membuat paket ini.' }
    $version = & $Python -I -c "import runpy, sys; print(runpy.run_path(sys.argv[1])['APP_VERSION'])" (Join-Path $repo 'src\msshi_doctor_app\constants.py')
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Versi Doctor tidak dapat dibaca.' }

    $rustDir = Join-Path $work 'rust'
    & cargo build --locked --offline --release --manifest-path (Join-Path $repo 'tools\preflight\Cargo.toml') --target-dir $rustDir
    if ($LASTEXITCODE -ne 0) { throw 'Kompilasi pemeriksaan awal Rust gagal.' }
    $dist = Join-Path $work 'dist'
    & $Python -I -m PyInstaller --noconfirm --distpath $dist --workpath (Join-Path $work 'pyinstaller') (Join-Path $repo 'build\msshi-doctor.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Pembuatan aplikasi Doctor gagal.' }
    & $Python -I -m PyInstaller --noconfirm --distpath $dist --workpath (Join-Path $work 'pyinstaller-onefile') (Join-Path $repo 'build\msshi-doctor-portable.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Pembuatan biner 1-Klik MSSHI-Doctor-Portable-win-x64.exe gagal.' }

    $stage = Join-Path $dist 'MSSHI-Doctor'
    Copy-Item -LiteralPath (Join-Path $rustDir 'release\msshi-preflight.exe') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $repo 'msshi-doctor.cmd'), (Join-Path $repo 'LICENSE'), (Join-Path $repo 'README.md') -Destination $stage
    $docs = [IO.Directory]::CreateDirectory((Join-Path $stage 'docs')).FullName
    Copy-Item -LiteralPath (Join-Path $repo 'docs\panduan') -Destination $docs -Recurse
    $reports = [IO.Directory]::CreateDirectory((Join-Path $docs 'reports')).FullName
    Copy-Item -LiteralPath (Join-Path $repo 'docs\reports\Laporan_Implementasi_MSSHI_Doctor_App.pdf') -Destination $reports
    $assets = [IO.Directory]::CreateDirectory((Join-Path $stage 'assets')).FullName
    foreach ($image in 'msshi-doctor-icon-64.png', 'msshi-doctor-cover.webp', 'msshi-doctor-banner.jpg') {
        Copy-Item -LiteralPath (Join-Path $repo "assets\$image") -Destination $assets
    }
    $workflows = [IO.Directory]::CreateDirectory((Join-Path $stage '.github\workflows')).FullName
    Copy-Item -LiteralPath (Join-Path $repo '.github\workflows\check.yml') -Destination $workflows

    & (Join-Path $stage 'msshi-preflight.exe') --doctor-dir $stage
    if ($LASTEXITCODE -ne 0) { throw 'Struktur paket portabel gagal diperiksa.' }
    $console = & (Join-Path $stage 'msshi-doctor-cli.exe') --version --json
    if ($LASTEXITCODE -ne 0 -or ($console | ConvertFrom-Json).version -ne $version) {
        throw 'CLI portabel gagal melaporkan versi yang sesuai.'
    }

    $zipName = "MSSHI-Doctor-$version-windows-x64.zip"
    $archive = Join-Path $work $zipName
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($stage, $archive)
    $destZip = Join-Path $output $zipName
    $destExe = Join-Path $output 'MSSHI-Doctor-Portable-win-x64.exe'
    Move-Item -LiteralPath $archive -Destination $destZip -Force
    Move-Item -LiteralPath (Join-Path $dist 'MSSHI-Doctor-Portable-win-x64.exe') -Destination $destExe -Force
    $hashExe = (Get-FileHash -LiteralPath $destExe -Algorithm SHA256).Hash.ToLower()
    $hashZip = (Get-FileHash -LiteralPath $destZip -Algorithm SHA256).Hash.ToLower()
    @(
        "$hashExe  MSSHI-Doctor-Portable-win-x64.exe",
        "$hashZip  $zipName"
    ) | Out-File -Encoding ascii (Join-Path $output 'SHA256SUMS.txt')
    Write-Output $destZip
} finally {
    $resolvedWork = (Resolve-Path -LiteralPath $work).Path
    if ([IO.Path]::GetDirectoryName($resolvedWork) -eq $tempParent -and [IO.Path]::GetFileName($resolvedWork) -match '^msshi-doctor-build-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}
