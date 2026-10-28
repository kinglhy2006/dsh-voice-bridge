# 贾维斯语音助手 · 依赖安装（Windows / PowerShell）
# 用法：右键“使用 PowerShell 运行”，或：
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$jarvis = Join-Path $here 'jarvis'
$python = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'

Write-Host ''
Write-Host '=== 贾维斯语音助手 · 安装依赖 ===' -ForegroundColor Cyan

if (-not (Test-Path $python)) {
  Write-Host '[X] 没找到 DSH 自带的 Python：' -ForegroundColor Red
  Write-Host "    $python"
  Write-Host '    请先安装并至少启动一次 DeepSeek Harness。'
  exit 1
}
Write-Host "[1/3] Python 就位：$python"

Write-Host '[2/3] 安装 Python 依赖（国内镜像，首次约 1-3 分钟）...'
$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
& $python -m pip install --quiet --disable-pip-version-check `
  -i https://mirrors.aliyun.com/pypi/simple/ `
  sherpa-onnx sounddevice numpy pyttsx3 edge-tts zstandard
& $python -c "import sherpa_onnx, sounddevice, pyttsx3, edge_tts, zstandard; print('    依赖 OK')"

Write-Host '[3/3] 挑麦克风（请现在对着电脑说话 6 秒）...' -ForegroundColor Cyan
$out = & $python (Join-Path $jarvis 'mic_probe.py') 6 2>&1 | Out-String
Write-Host $out
$m = [regex]::Match($out, '建议使用设备序号：(\d+)')
if ($m.Success) {
  [IO.File]::WriteAllText((Join-Path $jarvis 'device.json'),
    ('{"device":' + $m.Groups[1].Value + '}'), (New-Object System.Text.UTF8Encoding($false)))
  Write-Host ("    已记录麦克风设备号：" + $m.Groups[1].Value)
} else {
  Write-Host '    [!] 没听清，先不写配置（用系统默认麦克风）' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '=== 还差两步 ===' -ForegroundColor Green
Write-Host '1) DSH → 插件面板 → 添加本地目录，填：'
Write-Host "   $here" -ForegroundColor Yellow
Write-Host '2) 重启 DSH，然后喊「贾维斯」试试。'
Write-Host ''
Write-Host '识别模型（228MB）由 DSH 自带语音输入下载：在 DSH 里点一次麦克风按钮让它准备一次即可。'
Write-Host '换声音：改 jarvis\voice.json 里的 voice 字段，重启 DSH。'
Write-Host ''
