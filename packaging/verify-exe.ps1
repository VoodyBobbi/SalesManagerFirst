param([string]$Exe='')
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
if(-not $Exe){$Exe=Join-Path $repo 'dist\SalesManagerFirst.exe'}
if(-not (Test-Path -LiteralPath $Exe)){throw 'EXE not found. Run build.ps1 first.'}
$versionText=Get-Content -LiteralPath (Join-Path $repo 'sales_manager\__init__.py') -Raw -Encoding UTF8
if($versionText -notmatch "__version__\s*=\s*'([^']+)'"){throw 'Application version missing.'}
$expectedVersion=$Matches[1]
$exeVersion=(Get-Item -LiteralPath $Exe).VersionInfo.ProductVersion
if($exeVersion -ne $expectedVersion){throw 'Windows EXE version does not match source version. Rebuild the application.'}
$manifestFiles=@()
$main=Join-Path $repo 'main.py'
$manifestFiles+=,[pscustomobject]@{path='main.py';sha256=(Get-FileHash -LiteralPath $main -Algorithm SHA256).Hash.ToLowerInvariant()}
$sourceRoot=Join-Path $repo 'sales_manager'
foreach($sourceFile in Get-ChildItem -LiteralPath $sourceRoot -Filter '*.py' -File -Recurse){
    $relative=$sourceFile.FullName.Substring($sourceRoot.Length+1).Replace('\','/')
    $manifestFiles+=,[pscustomobject]@{path=('sales_manager/'+$relative);sha256=(Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
}
foreach($asset in @('assets\app.ico','ui\check.svg','ui\arrow-down.svg','ui\arrow-up.svg','ai\certs\russian_trusted_root_ca.pem')){
    $assetPath=Join-Path $sourceRoot $asset
    $relative=$asset.Replace('\','/')
    $manifestFiles+=,[pscustomobject]@{path=('sales_manager/'+$relative);sha256=(Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$manifestFiles=@($manifestFiles | Sort-Object path)
$manifestJson=ConvertTo-Json -InputObject $manifestFiles -Compress
$manifestHasher=[Security.Cryptography.SHA256]::Create()
$expectedManifestHash=([BitConverter]::ToString($manifestHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($manifestJson)))).Replace('-','').ToLowerInvariant()
$manifestHasher.Dispose()
$verificationId=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
$results=Join-Path $repo ('build\smoke-exe\'+$verificationId)
$portable=Join-Path $results 'standalone'
New-Item -ItemType Directory -Path $portable -Force | Out-Null
$copy=Join-Path $portable 'SalesManagerFirst.exe'
Copy-Item -LiteralPath $Exe -Destination $copy
New-Item -ItemType Directory -Path $results -Force | Out-Null
$savedPath=$env:PATH
$savedPythonPath=$env:PYTHONPATH
$savedPythonHome=$env:PYTHONHOME
$savedQtPlatform=$env:QT_QPA_PLATFORM
$savedQtFontdir=$env:QT_QPA_FONTDIR
$savedTemp=$env:TEMP
$savedTmp=$env:TMP
try {
    $env:TEMP=Join-Path $results 'runtime-temp'
    $env:TMP=$env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    $env:PATH=$env:WINDIR+'\System32;'+$env:WINDIR
    $env:PYTHONPATH=''
    $env:PYTHONHOME=''
    # The child Qt process opens its own window despite Start-Process -WindowStyle Hidden.
    # Offscreen still exercises rendering and captures the three diagnostic screenshots.
    $env:QT_QPA_PLATFORM='offscreen'
    $env:QT_QPA_FONTDIR=Join-Path $env:WINDIR 'Fonts'
    foreach($launch in 1..2){
        $process=Start-Process -FilePath $copy -ArgumentList ('--smoke-test "'+$results+'"') -WorkingDirectory $portable -WindowStyle Hidden -PassThru
        if(-not $process.WaitForExit(30000)){throw 'EXE did not finish diagnostics within 30 seconds.'}
        $process.WaitForExit()
        if($process.ExitCode -ne 0){throw ('EXE diagnostic exit code: '+$process.ExitCode)}
        $report=Get-Content -LiteralPath (Join-Path $results 'result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if(-not $report.ok -or -not $report.frozen -or -not $report.application_icon){throw 'Standalone diagnostic failed.'}
        if($report.application_version -ne $expectedVersion){throw 'EXE is older than the source application version.'}
        if($report.source_manifest_sha256 -ne $expectedManifestHash){throw 'EXE packaged source does not match the current source tree. Rebuild the application.'}
        if($launch -eq 1 -and $report.previous_launch_persistence){throw 'Diagnostics did not start with isolated fresh data.'}
        if($launch -eq 2 -and -not $report.previous_launch_persistence){throw 'Data did not survive process restart.'}
    }
    $report | Add-Member -NotePropertyName verified_source_manifest_sha256 -NotePropertyValue $expectedManifestHash -PassThru |
        Add-Member -NotePropertyName authenticode_status -NotePropertyValue ([string](Get-AuthenticodeSignature -FilePath $Exe).Status) -PassThru |
        ConvertTo-Json -Depth 5
} finally {
    $env:PATH=$savedPath
    $env:PYTHONPATH=$savedPythonPath
    $env:PYTHONHOME=$savedPythonHome
    $env:QT_QPA_PLATFORM=$savedQtPlatform
    $env:QT_QPA_FONTDIR=$savedQtFontdir
    $env:TEMP=$savedTemp
    $env:TMP=$savedTmp
}
