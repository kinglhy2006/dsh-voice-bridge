# 第三方模型与数据

## models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01

- 来源：https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
- 提供方：k2-fsa / icefall（WenetSpeech L 训练）
- 许可证：Apache License 2.0（见模型目录内 README.md）
- 用途：中文唤醒词检测（关键词检测模型，约 3.3M 参数）

## 语音识别模型（SenseVoice）

- 不随本插件分发。
- 由 DSH 自带的语音输入功能下载，位置：`%USERPROFILE%\.dsh\speech-to-text\sensevoice\models\sensevoice-onnx`
- 来源：FunAudioLLM/SenseVoice（ModelScope / Hugging Face）

## 语音合成

- 默认使用微软 Edge 在线语音（edge-tts），仅发送需要朗读的文本，不上传音频。
- 失败时退回 Windows 自带的 SAPI 语音（完全本地）。
