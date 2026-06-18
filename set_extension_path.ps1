param(
    [string]$ExtensionPath = "",
    [string]$IsaacSimLauncher = "C:\NVIDIA_Isaac_Sim\isaac-sim.bat",
    [switch]$Launch
)

if (-not $ExtensionPath) {
    $ExtensionPath = Split-Path -Parent $PSScriptRoot
}

$normalizedPath = Resolve-Path -Path $ExtensionPath
$env:OMNI_KIT_EXTENSION_PATH = $normalizedPath

Write-Host "Extension-Pfad gesetzt für diese PowerShell-Session:" -ForegroundColor Green
Write-Host $normalizedPath

if (Test-Path $IsaacSimLauncher) {
    Write-Host "Isaac Sim Launcher gefunden:" -ForegroundColor Green
    Write-Host $IsaacSimLauncher
    Write-Host "" -ForegroundColor Gray
    Write-Host "Startbefehl:" -ForegroundColor Cyan
    Write-Host "& \"$IsaacSimLauncher\""

    if ($Launch) {
        Write-Host "Starte Isaac Sim..." -ForegroundColor Green
        Start-Process -FilePath $IsaacSimLauncher
    }
} else {
    Write-Host "Isaac Sim Launcher nicht gefunden:" -ForegroundColor Yellow
    Write-Host $IsaacSimLauncher
    Write-Host "Gebe den richtigen Pfad mit -IsaacSimLauncher an." -ForegroundColor Yellow
}
