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
import collections
import io
import json
import os
import re
import sys
import time

try:
    import numpy as np
except ImportError:  # 只有真处理音频时才必需；缺了给明确提示，别直接炸在 import 行
    np = None


def _sherpa():
    try:
        import sherpa_onnx
    except ImportError:
        raise SystemExit("缺 sherpa-onnx，先跑一次 install.ps1")
    return sherpa_onnx


def _require_numpy():
    if np is None:
        raise SystemExit("缺 numpy，先跑一次 install.ps1")


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
# ponytail: 固定 3 倍增益，麦克风离得远/音量小也能听清；换设备或觉得太灵敏就调这里
GAIN = 3.0



def build_kws(keywords_file):
    sherpa_onnx = _sherpa()
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
    sherpa_onnx = _sherpa()
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
RECORD_FLAG = os.path.join(HERE, "recording.flag")
FOLLOWUP_FLAG = os.path.join(HERE, "followup.flag")
VOICE_CFG = os.path.join(HERE, "voice.json")
FOLLOWUP_DEFAULT = 25.0


def followup_seconds():
    """一句回答念完之后，再免唤醒听多少秒（0 = 关掉，每次都要喊名字）。

    谷歌「继续对话」、亚马逊 Follow-up 都是这个思路：趁你还没走开，
    留一小段时间不用喊唤醒词。voice.json 里写 followup_seconds 可改。
    """
    try:
        with io.open(VOICE_CFG, "r", encoding="utf-8") as f:
            value = float(json.load(f).get("followup_seconds", FOLLOWUP_DEFAULT))
    except Exception:
        return FOLLOWUP_DEFAULT
    return max(0.0, min(value, 120.0))


def set_recording(on):
    """录音时写个标记，界面靠它显示「录音中」。录完立刻删，界面才知道该切走了。"""
    try:
        if on:
            with io.open(RECORD_FLAG, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        elif os.path.exists(RECORD_FLAG):
            os.remove(RECORD_FLAG)
    except Exception:
        pass


def set_followup(on):
    """免唤醒窗口开着时留个标记，界面显示「直接说」。"""
    try:
        if on:
            with io.open(FOLLOWUP_FLAG, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        elif os.path.exists(FOLLOWUP_FLAG):
            os.remove(FOLLOWUP_FLAG)
    except Exception:
        pass

RESUME_WORDS = {"续", "继续", "继续吧", "继续啊", "接着", "接着吧", "接着说", "继续讲", "继续刚才的", "往下说", "讲下去", "再继续", "接着说下去"}


WAKE_PREFIXES = ("你好贾维斯", "你好贾维", "贾维斯", "贾维", "小贾")

# 「停」类指令：只用来打断朗读，绝不发给模型
STOP_WORDS = {
    "停", "停停", "停下", "停一下", "暂停", "打住", "别念", "别念了", "不要念", "不要念了",
    "不用念", "不用念了", "别说了", "闭嘴", "安静", "住口", "消停",
}
STOP_HINTS = ("别念", "不要念", "不用念", "闭嘴", "安静", "住口", "停")


def _strip_wake(text):
    """去掉开头的称呼，「贾维斯，继续」和「继续」要能分到一起。"""
    cleaned = re.sub(r"[\s。，、！？!?.,]", "", text or "")
    for prefix in WAKE_PREFIXES:
        if cleaned.startswith(prefix):
            return cleaned[len(prefix):]
    return cleaned


def is_stop_command(text):
    """「停 / 别念了 / 闭嘴」这类话是打断指令，不能当成问题发给模型。"""
    cleaned = _strip_wake(text)
    if not cleaned:
        return False
    if cleaned in STOP_WORDS:
        return True
    return len(cleaned) <= 6 and any(hint in cleaned for hint in STOP_HINTS)


def is_resume_command(text):
    """用户说「继续」这类短指令时，交给朗读程序接着念，不打扰模型。

    口头上常常连着说"贾维斯，继续"，所以先去掉开头的称呼再判断。
    """
    cleaned = _strip_wake(text)
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
    _require_numpy()
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


def rotate_log(path, limit=5 * 1024 * 1024):
    """日志超过 5MB 就改名一次，长期开着别把盘塞满。"""
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except Exception:
        pass


def run_live(args):
    import sounddevice as sd

    _require_numpy()
    kws = build_kws(args.keywords)
    asr = build_asr()
    transcript = os.path.join(HERE, "transcript.log")
    rotate_log(transcript)
    log = io.open(transcript, "a", encoding="utf-8", newline="\n")
    print("已就绪：先说「你好贾维斯」，听到叮声后说你要说的话。", flush=True)
    set_recording(False)  # 上次要是崩在录音中途，会留下一个过期的标记
    set_followup(False)

    stream = kws.create_stream()
    listening = False
    speech_started = False
    buf = []
    silence = 0.0
    started = 0.0
    loud = 0.0
    wake_at = 0.0
    peak_seen = 0.0
    was_speaking = False
    follow_until = 0.0
    onset = 0.0
    onset_rms = max(args.silence_rms * 1.5, 0.008)  # 比判定「说完」的门槛高一点，免得风扇声误触发
    recent = collections.deque(maxlen=6)  # 最近 0.6 秒，免唤醒起头时补回去

    def i_am_speaking():
        """朗读程序正在放音时，麦克风会听到自己的声音——这段时间不做唤醒判断。"""
        try:
            return (time.time() - os.path.getmtime(SPEAK_FLAG)) < 1.5
        except Exception:
            return False

    def finish():
        nonlocal buf, listening, silence, started, speech_started
        set_recording(False)  # 收音一停，界面上就该从「录音中」切成「思考中」
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
                if text:
                    beep(523, 80)  # 「咚」：这段说完了，开始识别/发送
                if text and not args.no_publish:
                    if not re.sub(r"[^\w\u4e00-\u9fff]", "", text):
                        print("· 只识别到标点，忽略这次", flush=True)
                        text = ""
                if text and not args.no_publish:
                    if is_stop_command(text):
                        # 「停 / 别念了」是打断指令，只让朗读闭嘴，不进对话
                        try:
                            with io.open(STOP_FLAG, "w", encoding="utf-8") as f:
                                f.write(str(time.time()))
                            print("· 收到「停」，不念了（没发给模型）", flush=True)
                        except Exception:
                            pass
                    elif is_resume_command(text):
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
                    now = time.time()
                    if speaking_now:
                        # 正在念：免唤醒窗口先收起，这会儿只认「你好贾维斯」来打断
                        was_speaking = True
                        if follow_until:
                            follow_until = 0.0
                            set_followup(False)
                    elif was_speaking:
                        # 刚念完：开一个免唤醒窗口，这段里直接开口问，不用喊名字
                        was_speaking = False
                        window = followup_seconds()
                        if window > 0:
                            follow_until = now + window
                            onset = 0.0
                            set_followup(True)
                            print("· 免唤醒窗口 %.0f 秒：直接说话就行" % window, flush=True)
                    if follow_until and (now > follow_until or not os.path.exists(FOLLOWUP_FLAG)):
                        # 到点了，或者界面那边发现你在打字、把标记删了 —— 都退出免唤醒
                        follow_until = 0.0
                        set_followup(False)
                    recent.append(frame)
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
                            set_recording(True)
                            started = time.time()
                            buf, silence, peak_seen = [], 0.0, 0.0
                            follow_until, onset = 0.0, 0.0
                            set_followup(False)
                            recent.clear()
                            break
                    # 免唤醒：窗口内连着听到 0.4 秒人声就直接开始录，不用再喊名字
                    if not listening and not speaking_now and follow_until > now:
                        rms_now = float(np.sqrt(np.mean((frame * GAIN) ** 2)))
                        onset = onset + BLOCK / SAMPLE_RATE if rms_now >= onset_rms else 0.0
                        if onset >= 0.4:
                            beep()
                            wake_at = time.time()
                            print("· 免唤醒：听到你开口了", flush=True)
                            listening = True
                            set_recording(True)
                            started = time.time()
                            # 把刚才缓存的 0.6 秒补回去，起头的字不会被吃掉
                            buf = [np.ascontiguousarray(f * GAIN, dtype=np.float32) for f in recent]
                            silence, peak_seen, speech_started = 0.0, 0.0, True
                            follow_until, onset = 0.0, 0.0
                            set_followup(False)
                            recent.clear()
                            continue
                else:
                    # 录音阶段放大再判断/识别：说话轻的时候静音检测才不会误判成「说完了」
                    frame = np.ascontiguousarray(frame * GAIN, dtype=np.float32)
                    buf.append(frame)
                    rms = float(np.sqrt(np.mean(frame ** 2)))
                    if rms > peak_seen:
                        peak_seen = rms
                    now = time.time()
                    if rms >= args.silence_rms:
                        # 有声音：标记「已经开始说话」，并重置静音计时
                        if not speech_started:
                            speech_started = True
                        silence = 0.0
                    elif speech_started:
                        silence += BLOCK / SAMPLE_RATE
                    # 还没开口：等 grace 秒，超时就放弃这次唤醒
                    if not speech_started and (now - started) > args.grace_seconds:
                        print("· 没听到说话（最大音量 %.3f，门槛 %.3f），退回去等唤醒。" % (
                            peak_seen, args.silence_rms), flush=True)
                        set_recording(False)
                        buf, listening, silence, speech_started = [], False, 0.0, False
                        stream = kws.create_stream()
                        continue
                    done = (speech_started and silence >= args.silence_time) or \
                           (now - started) > args.max_seconds
                    if done:
                        finish()
                        speech_started = False
                        stream = kws.create_stream()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        log.close()
    return 0


def run_selftest():
    """自检：不碰麦克风、不装依赖也能跑，确认「停 / 继续」这类话分得对。"""
    should_stop = ["停", "贾维斯，停一下", "别念了。", "闭嘴", "贾维斯，安静", "不用念了"]
    should_not_stop = [
        "继续", "贾维斯", "今天天气怎么样", "停下来讲个笑话", "帮我停一下那个服务", "停战协议是什么",
    ]
    should_resume = ["继续", "贾维斯，继续", "接着说", "续"]
    should_not_resume = ["继续这个话题的深入分析", "贾维斯，帮我查一下续费日期"]

    for t in should_stop:
        assert is_stop_command(t), "该判成停：%s" % t
    for t in should_not_stop:
        assert not is_stop_command(t), "不该判成停：%s" % t
    for t in should_resume:
        assert is_resume_command(t), "该判成继续：%s" % t
    for t in should_not_resume:
        assert not is_resume_command(t), "不该判成继续：%s" % t

    print("selftest ok: 停 %d 例 / 非停 %d 例 / 继续 %d 例 / 非继续 %d 例" % (
        len(should_stop), len(should_not_stop), len(should_resume), len(should_not_resume)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--wav")
    ap.add_argument("--selftest", action="store_true", help="只自检文本分类逻辑，不碰麦克风")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--keywords", default=os.path.join(HERE, "keywords.txt"))
    ap.add_argument("--silence-rms", type=float, default=0.006)
    ap.add_argument("--silence-time", type=float, default=2.5)
    ap.add_argument("--max-seconds", type=float, default=45.0)
    ap.add_argument("--grace-seconds", type=float, default=8.0)
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return run_selftest()
    if args.wav:
        return run_wav(args)
    if args.live:
        return run_live(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
