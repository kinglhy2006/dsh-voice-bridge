# -*- coding: utf-8 -*-
"""贾维斯 · 第 3 步：把 DSH 最新一条回答念出来。

声音：优先用微软「晓晓」在线语音（edge-tts，接近真人）；没网或失败时退回
      Windows 自带 SAPI（机械音，但一定能出声）。

文本来源：DSH 把每条消息写进本机会话文件
      ~/.dsh/sessions/<工作区>/session-<id>/session.v4.jsonl.zstd
    本程序盯住「最近被修改的会话文件」，出现新的 assistant/message 就念。

用法：
    python jarvis_speak.py            # 一直盯着
    python jarvis_speak.py --once     # 只打印当前最新一条（测试）
    python jarvis_speak.py --list     # 列出看到的会话文件
    python jarvis_speak.py --say 文本 # 直接念一句（测试声音）
"""
import argparse
import asyncio
import ctypes
import glob
import io
import json
import os
import re
import sys
import threading
import time

import zstandard

SESSIONS_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "speak.log")
TMP_MP3 = os.path.join(HERE, "_speak_%d.mp3" % os.getpid())
SPEAK_FLAG = os.path.join(HERE, "speaking.flag")
STOP_FLAG = os.path.join(HERE, "stop_speaking.flag")
RESUME_FLAG = os.path.join(HERE, "resume.flag")
REMAINDER = os.path.join(HERE, "interrupted.json")
REQUEST_FILE = os.path.join(HERE, "speak_request.json")
VOICE_TURN = os.path.join(HERE, "voice_turn.json")

VOICE = "zh-CN-XiaoxiaoNeural"   # 晓晓；换男声可用 zh-CN-YunxiNeural
RATE = "+10%"

QUEUE = []
LOCK = threading.Lock()
STOP = threading.Event()


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 播放 / 合成
def touch_flag():
    """给监听那边打标记：我在放音，别把自己的声音当成唤醒词。"""
    try:
        with io.open(SPEAK_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def play_mp3(path):
    touch_flag()
    try:
        return _play_mp3(path)
    finally:
        touch_flag()


def _play_mp3(path):
    """用 Windows 自带的 MCI 播 mp3，边放边看要不要被打断。

    返回：
      'ok'      放完了
      'stopped' 被打断（有人喊贾维斯）
      'error'   打不开文件
    """
    winmm = ctypes.WinDLL("winmm")
    alias = "jv%d" % os.getpid()
    buf = ctypes.create_unicode_buffer(128)
    winmm.mciSendStringW("close %s" % alias, None, 0, None)
    device_type = "waveaudio" if str(path).lower().endswith(".wav") else "mpegvideo"
    rc = winmm.mciSendStringW(
        'open "%s" type %s alias %s' % (path, device_type, alias), buf, 128, None
    )
    if rc != 0:
        return "error"
    result = "ok"
    try:
        # 异步播放，每 120ms 看一眼：放完了吗？有人喊「贾维斯」要求打断吗？
        winmm.mciSendStringW("play %s" % alias, None, 0, None)
        mode = ctypes.create_unicode_buffer(32)
        while True:
            time.sleep(0.12)
            touch_flag()  # 持续刷新“我在说话”的时间戳，监听那边才敢判断打断
            if os.path.exists(STOP_FLAG):
                try:
                    os.remove(STOP_FLAG)
                except Exception:
                    pass
                winmm.mciSendStringW("stop %s" % alias, None, 0, None)
                result = "stopped"
                break
            mode.value = ""
            winmm.mciSendStringW("status %s mode" % alias, mode, 32, None)
            if mode.value.strip().lower() != "playing":
                break
    finally:
        winmm.mciSendStringW("close %s" % alias, None, 0, None)
    return result


def split_sentences(text, limit=90, min_chars=24):
    """按句号切开，但把太短的碎片并入下一句。

    太短的片段（比如“好。”“第一，”）单独去请求在线合成很容易失败，
    合并成 24 字以上能明显减少“没有收到音频”的概率。
    """
    parts = re.split(r"(?<=[。！？!?;；\n])", text)
    merged = []
    for p in parts:
        if not p.strip():
            continue
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += p
        else:
            merged.append(p)
    out, cur = [], ""
    for p in merged:
        if len(cur) + len(p) <= limit:
            cur += p
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out or [text]


VOICE_CFG = os.path.join(HERE, "voice.json")


def current_voice():
    """读音配置放在 voice.json，改了立刻生效，不用重启。"""
    voice, rate = VOICE, RATE
    try:
        with io.open(VOICE_CFG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg.get("voice"), str) and cfg["voice"]:
            voice = cfg["voice"]
        if isinstance(cfg.get("rate"), str) and cfg["rate"]:
            rate = cfg["rate"]
    except Exception:
        pass
    return voice, rate


PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def synth_edge(text, path):
    """把一句话合成成 mp3 文件（在线语音）。

    微软语音服务国内可直连，绕代理反而慢，所以这一段临时把代理变量摘掉。
    """
    import edge_tts

    voice, rate = current_voice()

    async def run():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(path)

    saved = {key: os.environ.get(key) for key in PROXY_KEYS}
    for key in PROXY_KEYS:
        os.environ.pop(key, None)
    old_no_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "*"
    try:
        asyncio.run(run())
    finally:
        os.environ.pop("NO_PROXY", None)
        if old_no_proxy is not None:
            os.environ["NO_PROXY"] = old_no_proxy
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def speak_edge(text):
    synth_edge(text, TMP_MP3)
    return play_mp3(TMP_MP3)


# ---- 本地兜底：Kokoro（离线神经语音，音色名和在线那套一致）----
KOKORO_DIR = os.path.join(HERE, "models", "kokoro-int8-multi-lang-v1_1")

# 在线音色名 -> Kokoro 说话人 ID
KOKORO_SID = {
    "zh-CN-XiaoxiaoNeural": 47, "zh-CN-XiaoyiNeural": 48,
    "zh-CN-YunjianNeural": 49, "zh-CN-YunxiNeural": 50,
    "zh-CN-YunxiaNeural": 51, "zh-CN-YunyangNeural": 52,
}
_KOKORO = {}


def kokoro_instance():
    if _KOKORO.get("tts") is not None:
        return _KOKORO["tts"]
    import sherpa_onnx

    if not os.path.isdir(KOKORO_DIR):
        raise RuntimeError("本地语音模型不存在: " + KOKORO_DIR)
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=os.path.join(KOKORO_DIR, "model.int8.onnx"),
                voices=os.path.join(KOKORO_DIR, "voices.bin"),
                tokens=os.path.join(KOKORO_DIR, "tokens.txt"),
                data_dir=os.path.join(KOKORO_DIR, "espeak-ng-data"),
                dict_dir=os.path.join(KOKORO_DIR, "dict"),
                lexicon=",".join(
                    [
                        os.path.join(KOKORO_DIR, "lexicon-us-en.txt"),
                        os.path.join(KOKORO_DIR, "lexicon-zh.txt"),
                    ]
                ),
            ),
            num_threads=4,
            provider="cpu",
        ),
        max_num_sentences=1,
    )
    _KOKORO["tts"] = sherpa_onnx.OfflineTts(cfg)
    return _KOKORO["tts"]


def synth_kokoro(text, path):
    """本地合成到 wav，离线可用（比在线慢，但不会哑）。"""
    import numpy as np
    import wave as wave_mod

    voice, _ = current_voice()
    sid = KOKORO_SID.get(voice, 50)
    tts = kokoro_instance()
    audio = tts.generate(text, sid=sid, speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)
    with wave_mod.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.sample_rate)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())
    return path


def speak_sapi(text, cache={}):
    engine = cache.get("engine")
    if engine is None:
        import pyttsx3
        engine = pyttsx3.init()
        for v in engine.getProperty("voices"):
            if "zh" in (v.id or "").lower():
                engine.setProperty("voice", v.id)
                break
        engine.setProperty("rate", 185)
        cache["engine"] = engine
    engine.say(text)
    engine.runAndWait()


def speak(text):
    """边播边提前合成下一句，句与句之间不再等网络。

    生产者线程：按顺序把每句合成成 mp3，最多提前备 3 句。
    消费者（本线程）：一句一句播；被打断就丢掉后面的，把没念的存起来。
    """
    try:
        os.remove(STOP_FLAG)
    except Exception:
        pass

    sentences = [
        s for s in split_sentences(text) if re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    ]
    if not sentences:
        return

    import queue as _queue

    buf = _queue.Queue(maxsize=3)
    abort = {"v": False}
    cache = {}

    def producer():
        try:
            for i, sentence in enumerate(sentences):
                if abort["v"]:
                    break
                mp3 = os.path.join(HERE, "_speak_%d_%d.mp3" % (os.getpid(), i))
                wav = os.path.join(HERE, "_speak_%d_%d.wav" % (os.getpid(), i))
                # 先试在线（最自然），失败 3 次（隔 0.4/1.0/2.0 秒）再退本地
                ok = False
                for delay in (0.4, 1.0, 2.0):
                    try:
                        synth_edge(sentence, mp3)
                        ok = True
                        break
                    except Exception as exc:
                        cache["error"] = exc
                        time.sleep(delay)
                if ok:
                    buf.put((i, mp3))
                    continue
                # 在线失败：默认立刻用系统语音顶上（快，机械但不会让人干等）。
                # 想要本地神经语音（更自然但慢 20～40 秒）可设环境变量 JARVIS_TTS=kokoro。
                if os.environ.get("JARVIS_TTS", "").lower() == "kokoro":
                    try:
                        log("在线合成失败，改用本地神经语音（%s）" % sentence[:16])
                        synth_kokoro(sentence, wav)
                        buf.put((i, wav))
                        continue
                    except Exception as exc:
                        cache["error"] = exc
                log("在线合成失败，这句改用系统语音（%s）" % sentence[:16])
                buf.put((i, None))
        finally:
            buf.put(None)  # 收尾标记：念完就退出，别让消费端一直等

    threading.Thread(target=producer, daemon=True).start()

    while True:
        item = buf.get()
        if item is None:
            return
        index, path = item
        result = "error"
        if path is not None and os.path.exists(path):
            result = play_mp3(path)
            try:
                os.remove(path)
            except Exception:
                pass
        if result == "stopped":
            abort["v"] = True
            remainder = sentences[index:]
            save_remainder(remainder)
            log("被打断，停止朗读（还剩 %d 句，已存起来）" % len(remainder))
            return
        if result == "ok":
            continue
        if cache.get("error") is not None:
            log("在线语音重试后仍失败（改用系统语音）: %s" % cache.get("error"))
        try:
            speak_sapi(sentences[index])
        except Exception as exc:
            log("系统语音也失败: %s" % exc)
            return


# ---------------------------------------------------------------- 读取会话
def newest_session_file():
    pattern = os.path.join(SESSIONS_ROOT, "**", "session-*", "session.v4.jsonl.zstd")
    files = [f for f in glob.glob(pattern, recursive=True) if os.path.getsize(f) > 0]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def read_records(path):
    out = []
    try:
        with open(path, "rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh)
            text = io.TextIOWrapper(reader, encoding="utf-8")
            for line in text:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        log("读取会话失败: %s" % exc)
    return out


def strip_markdown(text):
    s = str(text)
    s = re.sub(r"```[\s\S]*?```", "。", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[#>*_~|]", "", s)
    s = re.sub(r"^\s*[-+]\s+", "", s, flags=re.M)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def assistant_texts(records):
    items = []
    for rec in records:
        if rec.get("type") != "assistant/message":
            continue
        seq = rec.get("seq")
        msg = (rec.get("data") or {}).get("message") or {}
        if msg.get("role") != "assistant":
            continue
        chunks = []
        for part in msg.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text") or ""
                if t.strip():
                    chunks.append(t)
        cleaned = strip_markdown(" ".join(chunks))
        if cleaned:
            items.append((seq, cleaned))
    return items


# ---------------------------------------------------------------- 被打断的剩余句子
def save_remainder(sentences):
    """被打断时把还没念的句子存起来，等你说「继续」接着念。"""
    try:
        payload = {"sentences": sentences, "at": time.time()}
        with io.open(REMAINDER, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        log("保存剩余句子失败: %s" % exc)


def take_remainder():
    """读取剩余句子并清掉，返回句子列表（没有就返回空）。"""
    try:
        with io.open(REMAINDER, "r", encoding="utf-8") as f:
            payload = json.load(f)
        os.remove(REMAINDER)
        sentences = payload.get("sentences") or []
        return [s for s in sentences if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def take_voice_turn():
    """判断这一轮是不是语音提问：界面插入语音文字后会写 voice_turn.json。

    有就用掉（念一次），没有就说明你是打字进来的 → 不念。
    """
    try:
        import json as _json

        with io.open(VOICE_TURN, "r", encoding="utf-8") as f:
            at = _json.load(f).get("at") or 0
        os.remove(VOICE_TURN)
        age = (time.time() * 1000 - float(at)) / 1000.0
        return 0 <= age <= 300
    except Exception:
        return False


def take_resume():
    """监听那边说了「继续」会写这个标记。"""
    try:
        age = time.time() - os.path.getmtime(RESUME_FLAG)
        if age > 120:
            os.remove(RESUME_FLAG)
            return False
        os.remove(RESUME_FLAG)
        return True
    except Exception:
        return False


def take_request():
    """外部（比如我调试时）可以把要念的文字写进 speak_request.json，由本进程统一朗读，
    避免再起第二个播放进程抢音频设备。"""
    try:
        with io.open(REQUEST_FILE, "r", encoding="utf-8") as f:
            text = json.load(f).get("text") or ""
        os.remove(REQUEST_FILE)
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


BEAT = {"t": time.time()}


def worker():
    while not STOP.is_set():
        BEAT["t"] = time.time()
        extra = take_request()
        if extra:
            log("念（外部请求）：%s" % extra[:40])
            speak(extra)
            continue
        if take_resume():
            try:
                age = time.time() - os.path.getmtime(RESUME_FLAG + ".stamp")
            except Exception:
                age = -1
            rest = take_remainder()
            if rest:
                log("继续念剩下的 %d 句（从收到指令到我开工 %.1fs）" % (len(rest), age))
                speak(" ".join(rest))
            else:
                log("收到「继续」，但没有存下的内容")
                speak("刚才那段已经念完了。")
            continue
        with LOCK:
            job = QUEUE.pop(0) if QUEUE else None
        if job is None:
            time.sleep(0.2)
            continue
        log("念：%s" % job[:40])
        speak(job)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只打印最新一条回答，不出声")
    ap.add_argument("--say", help="直接念一句话")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--interval", type=float, default=1.5)
    args = ap.parse_args()

    if args.say:
        speak(args.say)
        return 0

    if args.list:
        print("最新会话文件:", newest_session_file())
        return 0

    path = newest_session_file()
    if not path:
        log("没找到会话文件，退出")
        return 1

    items = assistant_texts(read_records(path))
    last_seq = max([s for s, _ in items], default=0)

    if args.once:
        if items:
            print(items[-1][1][:800])
        return 0

    log("盯住会话文件: %s（只念这之后的新回答）" % os.path.basename(os.path.dirname(path)))
    threading.Thread(target=worker, daemon=True).start()

    tracked = path
    while True:
        time.sleep(args.interval)
        newest = newest_session_file()
        if newest and newest != tracked:
            tracked = newest
            items = assistant_texts(read_records(tracked))
            last_seq = max([s for s, _ in items], default=0)
            log("切换会话: %s" % os.path.basename(os.path.dirname(tracked)))
            continue
        for seq, text in assistant_texts(read_records(tracked)):
            if seq is None or seq <= last_seq:
                continue
            last_seq = seq
            if not take_voice_turn():
                log("这条是你打字问的，不念：%s" % text[:30])
                continue
            with LOCK:
                QUEUE.append(text)

        # 看门狗：有活要干、但朗读线程 30 秒没动静，说明它卡死了，重启一个
        with LOCK:
            pending = len(QUEUE)
        if pending > 0 and (time.time() - BEAT["t"]) > 30:
            log("朗读线程疑似卡住（还有 %d 条没念），重启线程" % pending)
            try:
                with io.open(STOP_FLAG, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
            time.sleep(0.5)
            try:
                os.remove(STOP_FLAG)
            except Exception:
                pass
            BEAT["t"] = time.time()
            threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        STOP.set()
        print("\n已停止。")
