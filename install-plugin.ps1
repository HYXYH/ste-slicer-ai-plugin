[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^\d+\.\d+$')]
    [string]$Version = '7.10',

    [string]$ConfigDirectory
)

$ErrorActionPreference = 'Stop'

$pluginSource = Join-Path $PSScriptRoot 'plugin\AIAssistant'
foreach ($requiredFile in @('plugin.json', '__init__.py', 'AIAssistant.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $pluginSource $requiredFile) -PathType Leaf)) {
        throw "Plugin source is incomplete: $requiredFile is missing in $pluginSource"
    }
}

if ([string]::IsNullOrWhiteSpace($ConfigDirectory)) {
    $roamingDirectory = [Environment]::GetFolderPath('ApplicationData')
    if ([string]::IsNullOrWhiteSpace($roamingDirectory)) {
        throw 'Cannot find the user AppData\Roaming directory. Specify -ConfigDirectory.'
    }
    $ConfigDirectory = Join-Path $roamingDirectory "steslicer\$Version"
}

$pluginSource = [IO.Path]::GetFullPath($pluginSource)
$pluginDestination = [IO.Path]::GetFullPath((Join-Path $ConfigDirectory 'plugins\AIAssistant'))
$sourcePrefix = $pluginSource.TrimEnd('\') + '\'
$destinationPrefix = $pluginDestination.TrimEnd('\') + '\'
if ($destinationPrefix.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $sourcePrefix.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Plugin source and destination must be separate directories.'
}

if (-not $PSCmdlet.ShouldProcess($pluginDestination, 'Install or update AI Assistant')) {
    return
}

$copiedFiles = 0
foreach ($sourceFile in Get-ChildItem -LiteralPath $pluginSource -Recurse -File) {
    $relativePath = $sourceFile.FullName.Substring($sourcePrefix.Length)
    if ($relativePath -match '(^|[\\/])__pycache__([\\/]|$)' -or
        $sourceFile.Extension -in @('.pyc', '.pyo')) {
        continue
    }
    $destinationFile = Join-Path $pluginDestination $relativePath
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destinationFile))
    Copy-Item -LiteralPath $sourceFile.FullName -Destination $destinationFile -Force
    $copiedFiles += 1
}

Write-Host "Installed AI Assistant to: $pluginDestination"
Write-Host "Copied files: $copiedFiles"
Write-Host 'Restart STE Slicer to load the installed version.'
