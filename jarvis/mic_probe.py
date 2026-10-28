# -*- coding: utf-8 -*-
"""麦克风体检：同时监听所有输入设备若干秒，打印每个设备的音量峰值。

用法：python mic_probe.py [秒数]
"""
import sys
import time

import numpy as np
import sounddevice as sd

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
SR = 16000


def candidates():
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append(idx)
    return out


def main():
    idxs = candidates()
    streams = []
    peaks = {}
    for i in idxs:
        try:
            s = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                               device=i, blocksize=1600)
            s.start()
            streams.append((i, s))
            peaks[i] = 0.0
        except Exception as exc:
            print("设备 %d 打不开：%s" % (i, exc))
    print("开始监听 %d 个设备，共 %.0f 秒…… 请现在说话（越大声越好，持续说）" % (len(streams), DURATION))
    t0 = time.time()
    while time.time() - t0 < DURATION:
        for i, s in streams:
            try:
                data, _ = s.read(1600)
                p = float(np.abs(data).max())
                if p > peaks[i]:
                    peaks[i] = p
            except Exception:
                pass
    for i, s in streams:
        s.stop()
        s.close()
    print("\n=== 结果 ===")
    ranked = sorted(peaks.items(), key=lambda kv: kv[1], reverse=True)
    for i, p in ranked:
        print("设备 %-3d 峰值 %.4f  %s" % (i, p, sd.query_devices(i)["name"][:50]))
    if ranked and ranked[0][1] > 0.02:
        print("\n建议使用设备序号：%d" % ranked[0][0])
    else:
        print("\n所有设备都没有明显声音：麦克风被静音、被占用，或者说话太轻。")


if __name__ == "__main__":
    main()
