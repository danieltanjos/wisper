# -*- coding: utf-8 -*-
"""Aferidor de WER nos fixtures. NAO e' test_*.py de proposito: carrega o modelo.

    .venv\\Scripts\\python.exe tests\\bench_wer.py

Le tests/fixtures/refs.txt (utf-8-sig: comeca com BOM), casa cada chave com o
wav cujo nome COMECA por ela (ptbr_dev1 -> ptbr_dev1_noisy.wav) e pula as
referencias sem wav (ptbr_plain). Normaliza tirando acento e pontuacao, porque as
referencias estao sem acento.
"""
from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.lower())


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def load_wav(path: str) -> np.ndarray:
    import soxr

    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, path
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            raw = raw.reshape(-1, w.getnchannels()).mean(axis=1)
        x = raw.astype(np.float32) / 32768.0
        if w.getframerate() != 16000:   # os fixtures sao 22050 Hz
            x = soxr.resample(x, w.getframerate(), 16000).astype(np.float32)
    return x


def main() -> int:
    from wispr import stt

    refs = {}
    with open(os.path.join(FIX, "refs.txt"), encoding="utf-8-sig") as fh:
        for line in fh:
            if "|" in line:
                key, ref = line.rstrip("\n").split("|", 1)
                refs[key.strip()] = ref.strip()
    wavs = sorted(f for f in os.listdir(FIX) if f.endswith(".wav"))

    eng = stt.Engine()
    print("backend=%s load=%.1fs warm=%.2fs" % (eng.backend, eng.load_s, eng.warm_s))
    total_err = total_ref = 0
    for key, ref in refs.items():
        match = [f for f in wavs if f.startswith(key)]
        if not match:
            print("%-11s (sem wav, pulado)" % key)
            continue
        audio = load_wav(os.path.join(FIX, match[0]))
        t0 = time.perf_counter()
        hyp = eng.transcribe(audio)
        took = time.perf_counter() - t0
        r, h = words(ref), words(hyp)
        err = edit_distance(r, h)
        total_err += err
        total_ref += len(r)
        print("%-11s WER %5.1f%%  rtf %.3f  %r" % (key, 100.0 * err / len(r), took / (len(audio) / 16000), hyp))
    print("MEDIA (ponderada) WER %.1f%%" % (100.0 * total_err / max(total_ref, 1)))
    eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
