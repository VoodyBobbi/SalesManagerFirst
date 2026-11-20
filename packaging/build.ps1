param(
    [ValidateSet('Nuitka','PyInstaller')][string]$Backend='PyInstaller',
    [string]$Python='',
    [string]$OutputDirectory='',
    [ValidatePattern('^([A-Fa-f0-9]{40})?$')][string]$SigningCertificateThumbprint='',
    [string]$TimestampUrl='http://timestamp.digicert.com',
    [string]$SignTool='signtool.exe'
)
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
if(-not $OutputDirectory){$OutputDirectory=Join-Path $repo 'dist'}
$destination=[IO.Path]::GetFullPath($OutputDirectory)
if(-not $Python){$Python=Join-Path $repo '.venv\Scripts\python.exe'}
if(-not (Test-Path -LiteralPath $Python)){throw 'Python environment missing. Create .venv and install requirements.txt first.'}
$version=& $Python -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))"
if($version -ne '3.12'){throw 'Build requires Python 3.12.'}
$appVersion=& $Python -c "import runpy,sys; print(runpy.run_path(sys.argv[1])['__version__'])" (Join-Path $repo 'sales_manager\__init__.py')
if($LASTEXITCODE -ne 0 -or $appVersion -notmatch '^\d+\.\d+\.\d+$'){throw 'Invalid application version.'}
$fileVersion=$appVersion+'.0'
# Keep release work and tool caches inside the project.
$buildRoot=Join-Path $repo 'build\packages'
$stage=Join-Path $buildRoot ([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'main.py') -Destination $stage
# Stage only application code and named public assets. Never copy local SQLite,
# settings, .env, logs, Credential Manager exports, or stray certificate files.
$sourceRoot=Join-Path $repo 'sales_manager'
$stageSource=Join-Path $stage 'sales_manager'
foreach($sourceFile in Get-ChildItem -LiteralPath $sourceRoot -Filter '*.py' -File -Recurse){
    $relative=$sourceFile.FullName.Substring($sourceRoot.Length+1)
    $target=Join-Path $stageSource $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $sourceFile.FullName -Destination $target
}
foreach($asset in @('assets\app.ico','ui\check.svg','ui\arrow-down.svg','ui\arrow-up.svg','ai\certs\russian_trusted_root_ca.pem')){
    $target=Join-Path $stageSource $asset
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot $asset) -Destination $target
}
# Embed a deterministic fingerprint of every packaged source and public asset.
# The runtime smoke report exposes it so version equality alone cannot pass.
$manifestFiles=@(Get-ChildItem -LiteralPath $stage -File -Recurse | ForEach-Object {
    [pscustomobject]@{path=$_.FullName.Substring($stage.Length+1).Replace('\','/');sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
} | Sort-Object path)
$manifestJson=ConvertTo-Json -InputObject $manifestFiles -Compress
$manifestHasher=[Security.Cryptography.SHA256]::Create()
$manifestHash=([BitConverter]::ToString($manifestHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($manifestJson)))).Replace('-','').ToLowerInvariant()
$manifestHasher.Dispose()
$manifestModule="SOURCE_MANIFEST_SHA256 = '$manifestHash'`nSOURCE_MANIFEST = $manifestJson`n"
$manifestModulePath=Join-Path $stageSource '_release_manifest.py'
Set-Content -LiteralPath $manifestModulePath -Value $manifestModule -Encoding UTF8
$manifestDirectory=Join-Path $repo 'build\release-manifests'
New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null
Set-Content -LiteralPath (Join-Path $manifestDirectory ($manifestHash+'.json')) -Value $manifestJson -Encoding UTF8
Push-Location $stage
$savedTemp=$env:TEMP
$savedTmp=$env:TMP
$savedPyinstallerConfig=$env:PYINSTALLER_CONFIG_DIR
$savedNuitkaCache=$env:NUITKA_CACHE_DIR
try {
    $env:TEMP=Join-Path $repo 'build\tool-temp'
    $env:TMP=$env:TEMP
    $env:PYINSTALLER_CONFIG_DIR=Join-Path $repo 'build\pyinstaller-cache'
    $env:NUITKA_CACHE_DIR=Join-Path $repo 'build\nuitka-cache'
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    $versionInfo=@'
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(__VERSION_TUPLE__), prodvers=(__VERSION_TUPLE__), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Argon'),
    StringStruct('FileDescription', 'SALES MANAGER FIRST'),
    StringStruct('FileVersion', '__APP_VERSION__.0'),
    StringStruct('InternalName', 'SalesManagerFirst'),
    StringStruct('OriginalFilename', 'SalesManagerFirst.exe'),
    StringStruct('ProductName', 'SALES MANAGER FIRST'),
    StringStruct('ProductVersion', '__APP_VERSION__')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
'@
    $versionInfo.Replace('__VERSION_TUPLE__',($appVersion.Replace('.',',')+',0')).Replace('__APP_VERSION__',$appVersion) | Set-Content -LiteralPath 'version-info.txt' -Encoding Ascii
    if($Backend -eq 'Nuitka'){
        & $Python -m nuitka --onefile --mingw64 --enable-plugin=pyside6 --assume-yes-for-downloads --windows-console-mode=disable --windows-icon-from-ico=sales_manager/assets/app.ico --include-package=sales_manager --include-module=keyring.backends.Windows --include-package=win32ctypes --nofollow-import-to=win32ctypes.tests --include-package=truststore --include-package=certifi --include-package=pymorphy3 --include-package=dawg2_python --include-package=pymorphy3_dicts_ru --include-package-data=pymorphy3_dicts_ru --include-data-files=sales_manager/ai/certs/russian_trusted_root_ca.pem=sales_manager/ai/certs/russian_trusted_root_ca.pem --include-data-files=sales_manager/assets/app.ico=sales_manager/assets/app.ico --include-data-files=sales_manager/ui/*.svg=sales_manager/ui/ --output-dir=output --output-filename=SalesManagerFirst.exe --company-name=Argon --product-name=SalesManagerFirst --file-version=$fileVersion --product-version=$fileVersion --jobs=4 main.py
        $built=Join-Path $stage 'output\SalesManagerFirst.exe'
    } else {
        & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name SalesManagerFirst --icon sales_manager/assets/app.ico --version-file version-info.txt --collect-submodules sales_manager --collect-submodules keyring.backends --collect-submodules truststore --collect-data certifi --collect-submodules pymorphy3 --collect-submodules dawg2_python --collect-all pymorphy3_dicts_ru --add-data 'sales_manager/ai/certs/russian_trusted_root_ca.pem;sales_manager/ai/certs' --add-data 'sales_manager/assets/app.ico;sales_manager/assets' --add-data 'sales_manager/ui/*.svg;sales_manager/ui' --exclude-module PySide6.QtTest --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --distpath output --workpath work --specpath . main.py
        $built=Join-Path $stage 'output\SalesManagerFirst.exe'
    }
    if($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $built)){throw "$Backend build failed."}
    if($SigningCertificateThumbprint){
        & $SignTool sign /sha1 $SigningCertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $built
        if($LASTEXITCODE -ne 0){throw 'SignTool failed.'}
        if((Get-AuthenticodeSignature -FilePath $built).Status -ne 'Valid'){throw 'Authenticode signature is invalid.'}
    }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $exePath=Join-Path $destination 'SalesManagerFirst.exe'
    Copy-Item -LiteralPath $built -Destination $exePath -Force
    $exeHash=(Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
    "$exeHash  SalesManagerFirst.exe" | Set-Content -LiteralPath (Join-Path $destination 'SHA256SUMS.txt') -Encoding Ascii
    $releaseInfo=[ordered]@{version=$appVersion;source_manifest_sha256=$manifestHash;exe_sha256=$exeHash;exe_bytes=(Get-Item -LiteralPath $exePath).Length;authenticode_status=[string](Get-AuthenticodeSignature -FilePath $exePath).Status;built_utc=[DateTime]::UtcNow.ToString('o')}
    $releaseInfo | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'release-info.json') -Encoding UTF8
    $releaseInfo | ConvertTo-Json
} finally {
    $env:TEMP=$savedTemp
    $env:TMP=$savedTmp
    $env:PYINSTALLER_CONFIG_DIR=$savedPyinstallerConfig
    $env:NUITKA_CACHE_DIR=$savedNuitkaCache
    Pop-Location
}

