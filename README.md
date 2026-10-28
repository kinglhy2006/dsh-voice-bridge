# dsh-voice-bridge · 贾维斯语音助手（DSH 插件）

给 [DSH](https://github.com/deepseek-ai) 加一套「喊名字就对话」的语音能力：

- 🎙 **唤醒词**：「贾维斯」——本地关键词检测（sherpa-onnx），不上传音频
- 📝 **语音转文字**：本地 SenseVoice，中文识别，说完自动填进输入框并发送
- 🔊 **朗读回答**：微软在线语音（晓晓/云希，接近真人），失败自动退回系统语音
- ✋ **可打断**：我念到一半喊「贾维斯」立刻停；说「续 / 继续」从断点接着念
- ⌨️ **打字不念**：只有用嘴问的才朗读，打字问的只出文字

## 安装

> 前提：Windows + 已安装 DSH（脚本要用 DSH 自带的 Python）。

1. 把本仓库下载/解压到**可写目录**（不要放 `C:\Program Files`），例如 `D:\Jarvis`。
2. 右键 `install.ps1` → 「使用 PowerShell 运行」。
   它会装 Python 依赖、让你说一句话挑麦克风、写 `jarvis\device.json`。
3. DSH → **插件面板** → 添加本地目录 → 填本文件夹根目录（含 `package.json` 那一层）。
4. **重启 DSH**。之后每次启动都会自动拉起「听」和「说」两个后台进程，关掉 DSH 一起退出。

## 用法

- 说「贾维斯」→ 听到“叮”→ 说你要做的事；说完停一下（短命令 0.7 秒收尾，长句 2 秒）。
- 文字自动填进输入框并**自动发送**（不想发就半秒内按退格删掉）。
- 我回答时用语音念；**打字问的不念**。
- 念到一半喊「贾维斯」可打断；再说「续 / 继续 / 接着」从断点续播。

## 换声音 / 调语速

改 `jarvis\voice.json`，重启 DSH：

```json
{"voice":"zh-CN-YunxiNeural","rate":"+10%"}
```

可选：`zh-CN-YunxiNeural` 云希（年轻男）、`zh-CN-YunjianNeural` 云健（成熟男）、
`zh-CN-YunyangNeural` 云扬（播报）、`zh-CN-XiaoxiaoNeural` 晓晓（女）。

## 换唤醒词

改 `jarvis\kw_raw.txt`（一行一个，格式 `贾维斯 @贾维斯`），然后：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py .\jarvis\make_keywords.py .\models\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01\tokens.txt .\jarvis\keywords.txt
```

灵敏度在 `jarvis\jarvis_listen.py` 的 `build_kws()` 里：
`keywords_score` 越大越容易触发，`keywords_threshold` 越小越容易触发。

## 目录

```
dsh-voice-bridge/
├─ install.ps1            一键装依赖 + 挑麦克风
├─ package.json / cordis.patch.yml
├─ lib/index.js           服务端：拉起两个 Python 进程、提供本地接口
├─ lib/client.js          界面：把文字原生插入输入框并发送（不抢焦点）
├─ jarvis/                Python 脚本、唤醒词、音色、设备配置
└─ models/                唤醒词模型（31MB）+ 第三方许可说明
```

## 隐私

- 唤醒词检测、语音识别**全部在本地**，音频不出机器。
- 只有「朗读」会把**文本**发给微软在线语音；断网会自动用 Windows 本地语音。
- 本地接口只监听本机，并拒绝跨站请求。

## 常见问题

| 现象 | 处理 |
|---|---|
| 喊了没反应 | 跑 `python jarvis\mic_probe.py 8` 看哪个设备有声音，把设备号写进 `jarvis\device.json` |
| 太容易误触发 | 把 `keywords_score` 调回 1.0～1.5 |
| 一直没声音 | 看 `jarvis\speak.log`；确认没有被别的程序占用扬声器 |
| 听不清 / 识别错 | 靠近麦克风，或在安静环境重试 |
| 装了两个同名插件 | 会各自拉起一套进程，务必只留一个 |

## 已知限制

- 只支持 Windows（用到了 MCI 播放、SAPI、DSH 自带 Python 路径）。
- 唤醒词对普通话口音敏感度有差异，可自行调灵敏度。
- `jarvis/voice.log`、`speak.log` 会持续增长，长期使用可定期清理。

## 许可

代码 MIT。第三方模型见 [models/NOTICE.md](models/NOTICE.md)。
