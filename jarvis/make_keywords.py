# -*- coding: utf-8 -*-
"""生成 sherpa-onnx KWS 的 keywords.txt（拼音：声母 + 韵母）。

输入文件 kw_raw.txt 每行一条，例如：
    贾维斯 :3.0 #0.35 @贾维斯
输出形如：
    j iǎ w éi s ī :3.0 #0.35 @贾维斯

用法：python make_keywords.py <tokens.txt> <输出文件>
"""
import io
import os
import sys

from pypinyin import Style, pinyin

INITIALS = [
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
    "y", "w",
]


def split_syllable(syl):
    """jiǎ -> ['j', 'iǎ']；zhōng -> ['zh', 'ōng']；ǎn -> ['ǎn']"""
    for ini in INITIALS:
        if syl.startswith(ini) and len(syl) > len(ini):
            return [ini, syl[len(ini):]]
    return [syl]


def phrase_tokens(phrase):
    toks = []
    for ch in phrase:
        py = pinyin(ch, style=Style.TONE, heteronym=False, errors="ignore")
        if not py or not py[0]:
            raise ValueError("无法取到拼音: %r" % ch)
        for syl in py[0]:
            toks.extend(split_syllable(syl))
    return toks


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    tokens_path, out_path = sys.argv[1], sys.argv[2]
    here = os.path.dirname(os.path.abspath(__file__))
    raw_path = os.path.join(here, "kw_raw.txt")

    known = set()
    with io.open(tokens_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                known.add(line.split()[0])

    results = []
    with io.open(raw_path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            words, extras = [], []
            for part in raw.split():
                if part.startswith((":", "#", "@")):
                    extras.append(part)
                else:
                    words.append(part)
            phrase = "".join(words).replace("_", " ").strip()
            if not phrase:
                continue
            toks = phrase_tokens(phrase)
            missing = [t for t in toks if t not in known]
            if missing:
                print("[warn] token 不在表里: %s（词 %s）" % (missing, phrase))
            line_out = " ".join(toks + extras)
            results.append(line_out)
            print("%s -> %s" % (phrase, line_out))

    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(results) + "\n")
    print("已写出 %s（%d 条）" % (out_path, len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
