# -*- coding: utf-8 -*-
"""贾维斯 · 第 1 步：听到唤醒词 → 录音 → 中文转文字（全离线）

流程：
  1) 麦克风常开，只跑关键词检测（很省 CPU）
  2) 听到「贾维斯」→ 叮一声 → 开始录音
  3) 检测到 1 秒静音（或最长 10 秒）→ 停止录音
  4) 用 SenseVoice 转文字，打印并追加到 transcript.log

用法：
  python jarvis_listen.py --live --device 2
  python jarvis_listen.py --wav 某段话.wav          # 只测识别
"""
import argparse
import io
import json
import os
import re
import sys
import time

import numpy as np
import sherpa_onnx

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_dir(relative, fallbacks=()):
    """在脚本目录、上一级目录、以及给定兜底路径里找目录，找不到返回 None。"""
    candidates = [os.path.join(HERE, relative), os.path.join(os.path.dirname(HERE), relative)]
    candidates.extend(fallbacks)
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None


KWS_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
MODEL_DIR = _find_dir(os.path.join("models", KWS_NAME)) or os.path.join(HERE, "models", KWS_NAME)

# SenseVoice 模型：优先用包内自带的，其次用 DSH 语音输入插件下载好的那份
SENSE_DIR = _find_dir(
    os.path.join("models", "sensevoice-onnx"),
    (
        os.path.join(
            os.path.expanduser("~"),
            ".dsh", "speech-to-text", "sensevoice", "models", "sensevoice-onnx",
        ),
    ),
)
SAMPLE_RATE = 16000
BLOCK = 1600  # 0.1 秒



def build_kws(keywords_file):
    return sherpa_onnx.KeywordSpotter(
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        encoder=os.path.join(MODEL_DIR, "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=os.path.join(MODEL_DIR, "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        joiner=os.path.join(MODEL_DIR, "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        keywords_file=keywords_file,
        num_threads=2,
        max_active_paths=4,
        num_trailing_blanks=1,
        keywords_score=2.5,        # 越大越容易触发（默认 1.0）
        keywords_threshold=0.10,   # 越小越容易触发（默认 0.25）
        provider="cpu",
    )


def build_asr(num_threads=4):
    if not SENSE_DIR:
        raise SystemExit(
            "找不到 SenseVoice 识别模型。请先跑一次 DSH 的语音输入（它会自动下载），"
            "或者把模型放到 jarvis\\..\\models\\sensevoice-onnx 目录下。"
        )
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(SENSE_DIR, "model.int8.onnx"),
        tokens=os.path.join(SENSE_DIR, "tokens.txt"),
        num_threads=num_threads,
        language="zh",
        use_itn=True,
        provider="cpu",
    )


def transcribe(asr, samples):
    stream = asr.create_stream()
    stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(samples, dtype=np.float32))
    asr.decode_stream(stream)
    return stream.result.text.strip()


def beep(freq=880, ms=120):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        print("\a", end="")


OUTBOX = os.path.join(HERE, "outbox.json")
SPEAK_FLAG = os.path.join(HERE, "speaking.flag")
STOP_FLAG = os.path.join(HERE, "stop_speaking.flag")
RESUME_FLAG = os.path.join(HERE, "resume.flag")
VOICE_TURN = os.path.join(HERE, "voice_turn.json")

RESUME_WORDS = {"续", "继续", "继续吧", "继续啊", "接着", "接着吧", "接着说", "继续讲", "继续刚才的", "往下说", "讲下去", "再继续", "接着说下去"}


WAKE_PREFIXES = ("贾维斯", "贾维", "小贾")


def is_resume_command(text):
    """用户说「继续」这类短指令时，交给朗读程序接着念，不打扰模型。

    口头上常常连着说"贾维斯，继续"，所以先去掉开头的称呼再判断。
    """
    cleaned = re.sub(r"[\s。，、！？!?.,]", "", text or "")
    for prefix in WAKE_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if cleaned in RESUME_WORDS:
        return True
    return len(cleaned) <= 4 and ("继续" in cleaned or cleaned.endswith("续"))


def publish(text):
    """把转写结果写进 outbox.json，DSH 的 dsh-voice-bridge 插件会取走并插入输入框。"""
    seq = 0
    try:
        with io.open(OUTBOX, "r", encoding="utf-8") as f:
            seq = int(json.load(f).get("seq", 0))
    except Exception:
        seq = 0
    payload = {"seq": seq + 1, "text": text, "ts": time.time()}
    tmp = OUTBOX + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, OUTBOX)
        return True
    except Exception as exc:
        print("写入 outbox 失败: %s" % exc, flush=True)
        return False


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("只支持 16bit 单声道 wav: " + path)
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    return sr, np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def run_wav(args):
    sr, samples = read_wav(args.wav)
    if sr != SAMPLE_RATE:
        raise SystemExit("采样率需 16000，实际 %d" % sr)
    asr = build_asr()
    t0 = time.time()
    text = transcribe(asr, samples)
    print("识别结果（%.2fs）：%s" % (time.time() - t0, text or "（空）"))
    return 0


def run_live(args):
    import sounddevice as sd

    kws = build_kws(args.keywords)
    asr = build_asr()
    log = io.open(os.path.join(HERE, "transcript.log"), "a", encoding="utf-8", newline="\n")
    print("已就绪：先说「贾维斯」，听到叮声后说你要说的话。", flush=True)

    stream = kws.create_stream()
    listening = False
    speech_started = False
    buf = []
    silence = 0.0
    started = 0.0
    loud = 0.0

    def i_am_speaking():
        """朗读程序正在放音时，麦克风会听到自己的声音——这段时间不做唤醒判断。"""
        try:
            return (time.time() - os.path.getmtime(SPEAK_FLAG)) < 1.5
        except Exception:
            return False

    def finish():
        nonlocal buf, listening, silence, started, speech_started
        if buf:
            audio = np.concatenate(buf)
            dur = len(audio) / float(SAMPLE_RATE)
            if dur > 0.3:
                t_rec = time.time()
                text = transcribe(asr, audio)
                print("· 时序：唤醒后收音 %.1fs，识别 %.1fs（共 %.1fs）" % (
                    t_rec - wake_at, time.time() - t_rec, time.time() - wake_at), flush=True)
                line = "%s [%.1fs] %s" % (time.strftime("%H:%M:%S"), dur, text or "（空）")
                print("你：" + (text or "（空）"), flush=True)
                log.write(line + "\n")
                log.flush()
                if text and not args.no_publish:
                    if not re.sub(r"[^\w\u4e00-\u9fff]", "", text):
                        print("· 只识别到标点，忽略这次", flush=True)
                        text = ""
                if text and not args.no_publish:
                    if is_resume_command(text):
                        # 「继续」是给朗读程序用的，不进对话、不麻烦模型
                        try:
                            now_ts = time.time()
                            with io.open(RESUME_FLAG, "w", encoding="utf-8") as f:
                                f.write(str(now_ts))
                            with io.open(RESUME_FLAG + ".stamp", "w", encoding="utf-8") as f2:
                                f2.write(str(now_ts))
                            print("· 让它接着刚才的话念", flush=True)
                        except Exception:
                            publish(text)
                    else:
                        publish(text)
                        # 同时留个标记：这一轮是语音来的，回答要念出来
                        try:
                            with io.open(VOICE_TURN, "w", encoding="utf-8") as fv:
                                fv.write(json.dumps({"at": int(time.time() * 1000)}))
                        except Exception:
                            pass
        buf, listening, silence, started, speech_started = [], False, 0.0, 0.0, False

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=BLOCK, device=args.device) as mic:
            while True:
                block, _ = mic.read(BLOCK)
                frame = np.ascontiguousarray(block.reshape(-1), dtype=np.float32)
                if not listening:
                    # 放音期间也照常做唤醒检测——这样你可以在我说到一半时打断我
                    speaking_now = i_am_speaking()
                    stream.accept_waveform(SAMPLE_RATE, frame)
                    peak = float(np.abs(frame).max())
                    if peak > 0.05:
                        loud += BLOCK / SAMPLE_RATE
                        if loud >= 1.2:
                            print("· 听到大声但没认出唤喊词（峰值 %.2f），继续监听" % peak, flush=True)
                            loud = 0.0
                    else:
                        loud = 0.0
                    while kws.is_ready(stream):
                        kws.decode_stream(stream)
                        if kws.get_result(stream):
                            loud = 0.0
                            if speaking_now:
                                # 打断：让正在放音的朗读立刻停嘴
                                try:
                                    with io.open(STOP_FLAG, "w", encoding="utf-8") as f:
                                        f.write(str(time.time()))
                                except Exception:
                                    pass
                            beep()
                            wake_at = time.time()
                            print("· 我在听…", flush=True)
                            listening = True
                            started = time.time()
                            buf, silence = [], 0.0
                            break
                else:
                    buf.append(frame)
                    rms = float(np.sqrt(np.mean(frame ** 2)))
                    now = time.time()
                    if rms >= args.silence_rms:
                        # 有声音：标记「已经开始说话」，并重置静音计时
                        if not speech_started:
                            speech_started = True
                            speech_at = now
                        silence = 0.0
                    elif speech_started:
                        silence += BLOCK / SAMPLE_RATE
                    # 还没开口：只等 grace 秒，超时就放弃这次唤醒
                    if not speech_started and (now - started) > args.grace_seconds:
                        print("· 没听到说话，退回去等唤醒。", flush=True)
                        buf, listening, silence, speech_started = [], False, 0.0, False
                        stream = kws.create_stream()
                        continue
                    done = (speech_started and silence >= args.silence_time) or \
                           (now - started) > args.max_seconds
                    # 短指令（说得很短）快速收尾：不让「续」「停」这类命令等满 2 秒
                    if (speech_started and not done and silence >= args.short_silence_time
                            and (now - speech_at) <= args.short_speech_seconds):
                        done = True
                    if done:
                        finish()
                        speech_started = False
                        stream = kws.create_stream()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        log.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--wav")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--keywords", default=os.path.join(HERE, "keywords.txt"))
    ap.add_argument("--silence-rms", type=float, default=0.012)
    ap.add_argument("--silence-time", type=float, default=2.0)
    ap.add_argument("--max-seconds", type=float, default=45.0)
    ap.add_argument("--grace-seconds", type=float, default=3.0)
    ap.add_argument("--short-silence-time", type=float, default=0.7)
    ap.add_argument("--short-speech-seconds", type=float, default=1.4)
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()
    if args.wav:
        return run_wav(args)
    if args.live:
        return run_live(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
