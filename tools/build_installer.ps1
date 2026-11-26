param(
    [string]$IsccPath = 'ISCC.exe',
    [switch]$SkipAppBuild
)

$ErrorActionPreference = 'Stop'
$compiler = Get-Command -Name $IsccPath -CommandType Application -ErrorAction Stop
$root = Split-Path -Parent $PSScriptRoot

if (-not $SkipAppBuild) {
    & (Join-Path $PSScriptRoot 'package.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$app = Join-Path $root 'dist/MotorBench'
foreach ($file in 'MotorBench.exe', '_internal/bundle/motor-bench-probe.exe', '_internal/bundle/f407.hex', '_internal/bundle/h723.hex') {
    if (-not (Test-Path -LiteralPath (Join-Path $app $file) -PathType Leaf)) {
        throw "安装文件缺失：$file，请先打包上位机。"
    }
}

& $compiler.Source (Join-Path $PSScriptRoot 'MotorBench.iss')
exit $LASTEXITCODE
