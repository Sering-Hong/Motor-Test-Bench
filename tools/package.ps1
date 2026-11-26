$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $localPackages = @('build/packager', 'build/python-deps') |
        Where-Object { Test-Path -LiteralPath $_ } |
        ForEach-Object { (Resolve-Path -LiteralPath $_).Path }
    if ($localPackages) {
        $env:PYTHONPATH = ($localPackages -join [IO.Path]::PathSeparator)
    }
    $probeBinary = Join-Path $root 'bundle/motor-bench-probe.exe'
    $icon = Join-Path $root 'src/motorbench.ico'
    if (-not (Test-Path -LiteralPath $probeBinary -PathType Leaf)) {
        throw '缺少 probe-rs 后端，请先构建 probe_bridge，并将 motor-bench-probe.exe 复制到 bundle。'
    }
    $options = @(
        '--noconfirm', '--onedir', '--windowed', '--name', 'MotorBench', '--icon', $icon,
        '--distpath', 'dist', '--workpath', 'build/pyinstaller', '--specpath', 'build',
        '--paths', (Join-Path $root 'src'), '--add-data', ((Join-Path $root 'src/style.qss') + ':src'),
        '--add-data', ($icon + ':src'),
        '--add-data', ((Join-Path $root '使用说明.md') + ':.'),
        '--add-binary', ($probeBinary + ':bundle')
    )
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $root 'bundle') -File) {
        if ($file.Extension -in '.hex', '.json') { $options += @('--add-data', ($file.FullName + ':bundle')) }
    }
    $options += 'src/main.py'
    # Windows PowerShell 5.1 把原生命令的 stderr 日志当作错误；只在此处放宽，仍按退出码判断。
    $ErrorActionPreference = 'Continue'
    python -m PyInstaller @options 2>&1 | ForEach-Object { "$_" }
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Copy-Item -LiteralPath '使用说明.md', 'LICENSE', 'THIRD_PARTY.md', 'tools/LGPL-3.0.txt', 'tools/Apache-2.0.txt', 'tools/Inno-Setup-License.txt' -Destination 'dist/MotorBench'
} finally {
    Pop-Location
}
