# -*- coding: utf-8 -*-
"""Testes de wispr.audio.to_whisper() em sinais sinteticos.

Nenhum microfone e aberto: `_safety` troca `sounddevice` por um stub antes do
import, entao `Pa_Initialize` nunca roda e nenhum endpoint e tocado. So numpy,
soxr e scipy trabalham aqui -- CPU pura, zero VRAM.

A cadeia medida em docs/ARCHITECTURE.md secao 4 e:
    soxr 48k->16k  ->  passa-alta Butterworth 4a ordem em 80 Hz  ->  pico -3 dBFS
com a normalizacao guardada por `if peak > 1e-5`, para nunca amplificar silencio.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.dirname(_HERE), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import _safety  # noqa: E402  stubs antes de qualquer import de wispr

import inspect
import math
import unittest

from wispr import config

_MISSING_DEPS = [n for n in ("numpy", "soxr", "scipy") if not _safety.has_module(n)]

if not _MISSING_DEPS:
    import numpy as np

_audio, _why = _safety.load_pure("wispr.audio")
to_whisper = getattr(_audio, "to_whisper", None) if _audio else None

try:
    _TAKES_CFG = bool(to_whisper) and "cfg" in inspect.signature(to_whisper).parameters
except (TypeError, ValueError):  # pragma: no cover
    _TAKES_CFG = False


def whisper(x):
    """to_whisper() preso nos DEFAULTS.

    Sem isso o teste leria o config.json do usuario: quem tiver mexido em
    `highpass_hz` ou `normalize_dbfs` veria a suite falhar sem ter bug nenhum.
    """
    if _TAKES_CFG:
        return to_whisper(x, config.Config(dict(config.DEFAULTS)))
    return to_whisper(x)

CAPTURE_SR = getattr(_audio, "CAPTURE_SR", 48000) if _audio else 48000
TARGET_SR = getattr(_audio, "TARGET_SR", 16000) if _audio else 16000

#: -3 dBFS em amplitude linear.
PEAK_TARGET = 10.0 ** (-3.0 / 20.0)  # 0.7079


def setUpModule():
    if _MISSING_DEPS:
        raise unittest.SkipTest(
            "dependencias de DSP ausentes: %s. Rode scripts\\setup.ps1 (a suite "
            "nao instala nada por conta propria)." % ", ".join(_MISSING_DEPS)
        )
    if to_whisper is None:
        raise unittest.SkipTest(_why or "wispr.audio nao expoe to_whisper()")


def tone(freq, seconds=1.0, amp=0.5, sr=48000, phase=0.0):
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    return (amp * np.sin(2 * math.pi * freq * t + phase)).astype(np.float32)


class ShapeTest(unittest.TestCase):

    def test_output_is_one_third_of_the_input(self):
        out = whisper(tone(1000, 1.0))
        self.assertAlmostEqual(len(out), TARGET_SR, delta=4,
                               msg="48k->16k deveria dar 1/3 das amostras")

    def test_several_lengths(self):
        for seconds in (0.03, 0.25, 2.0):
            with self.subTest(seconds=seconds):
                x = tone(1000, seconds)
                out = whisper(x)
                self.assertAlmostEqual(len(out), len(x) // 3, delta=4)

    def test_dtype_is_float32(self):
        self.assertEqual(whisper(tone(1000, 0.2)).dtype, np.float32)

    def test_output_is_mono_and_finite(self):
        out = whisper(tone(1000, 0.2))
        self.assertEqual(out.ndim, 1)
        self.assertTrue(np.all(np.isfinite(out)))

    def test_input_is_not_mutated(self):
        x = tone(1000, 0.2, amp=0.9)
        antes = x.copy()
        whisper(x)
        np.testing.assert_array_equal(x, antes)

    def test_empty_input_does_not_crash(self):
        out = whisper(np.zeros(0, dtype=np.float32))
        self.assertEqual(len(out), 0)

    def test_output_stays_inside_the_unit_range(self):
        out = whisper(tone(1000, 0.5, amp=0.99))
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)


class SilenceGuardTest(unittest.TestCase):
    """`if peak > 1e-5` existe para silencio puro nunca virar ruido a -3 dBFS."""

    def test_digital_silence_stays_silent(self):
        out = whisper(np.zeros(CAPTURE_SR, dtype=np.float32))
        self.assertLess(float(np.max(np.abs(out))), 1e-6,
                        "silencio puro foi amplificado")

    def test_below_the_guard_is_not_amplified(self):
        out = whisper(tone(1000, 1.0, amp=1e-6))
        self.assertLess(float(np.max(np.abs(out))), 1e-4,
                        "sinal abaixo do gate de 1e-5 foi normalizado")

    def test_silence_output_has_no_nans(self):
        out = whisper(np.zeros(CAPTURE_SR // 2, dtype=np.float32))
        self.assertTrue(np.all(np.isfinite(out)), "divisao por zero no normalize")


class NormalisationTest(unittest.TestCase):

    def _peak(self, x):
        return float(np.max(np.abs(whisper(x))))

    def test_loud_input_is_brought_down_to_minus_3_dbfs(self):
        self.assertAlmostEqual(self._peak(tone(1000, 1.0, amp=0.95)),
                               PEAK_TARGET, delta=0.03)

    def test_quiet_input_is_brought_up_to_minus_3_dbfs(self):
        """Whisper nao e invariante a ganho: as features deslocam 0,5 por decada."""
        self.assertAlmostEqual(self._peak(tone(1000, 1.0, amp=0.01)),
                               PEAK_TARGET, delta=0.03)

    def test_two_takes_at_different_levels_end_up_at_the_same_peak(self):
        alto = self._peak(tone(1000, 1.0, amp=0.8))
        baixo = self._peak(tone(1000, 1.0, amp=0.05))
        self.assertAlmostEqual(alto, baixo, delta=0.02)

    def test_no_clipping_after_normalisation(self):
        self.assertLess(self._peak(tone(1000, 1.0, amp=1.0)), 1.0)


class HighpassTest(unittest.TestCase):
    """Passa-alta de 80 Hz: mata rumble de mesa e pop de ar sem tocar na voz.

    A normalizacao vem depois do filtro, entao comparar amplitudes absolutas nao
    diz nada -- o teste olha o espectro de um sinal com os dois tons juntos.
    """

    N = 14400  # 0,9 s a 16 kHz: 40 Hz e 1000 Hz caem em bins inteiros (36 e 900)

    def _bins(self, out):
        seg = out[-self.N:]
        self.assertEqual(len(seg), self.N, "saida curta demais para a FFT")
        spec = np.abs(np.fft.rfft(seg.astype(np.float64)))
        return spec

    def setUp(self):
        x = tone(40, 1.0, amp=0.4) + tone(1000, 1.0, amp=0.4)
        self.spec = self._bins(whisper(x.astype(np.float32)))
        self.bin40 = 36
        self.bin1k = 900

    def test_bins_land_where_expected(self):
        self.assertAlmostEqual(self.bin40 * TARGET_SR / self.N, 40.0, places=6)
        self.assertAlmostEqual(self.bin1k * TARGET_SR / self.N, 1000.0, places=6)

    def test_40hz_is_strongly_attenuated(self):
        """Butterworth 4a ordem em 80 Hz da cerca de -24 dB em 40 Hz."""
        ratio = float(self.spec[self.bin40] / self.spec[self.bin1k])
        self.assertLess(ratio, 0.2,
                        "40 Hz sobreviveu (razao %.4f); o passa-alta nao rodou" % ratio)

    def test_1khz_is_preserved(self):
        self.assertEqual(int(np.argmax(self.spec)), self.bin1k,
                         "o pico do espectro deixou de ser 1 kHz")

    def test_1khz_dominates_the_energy(self):
        total = float(np.sum(self.spec ** 2))
        self.assertGreater(float(self.spec[self.bin1k] ** 2) / total, 0.8)

    def test_a_pure_1khz_tone_keeps_its_shape(self):
        out = whisper(tone(1000, 1.0, amp=0.5))
        seg = out[-self.N:].astype(np.float64)
        rms = float(np.sqrt(np.mean(seg ** 2)))
        peak = float(np.max(np.abs(seg)))
        # Senoide: RMS = pico / sqrt(2). Se o filtro distorcesse, isso se afasta.
        self.assertAlmostEqual(rms, peak / math.sqrt(2), delta=0.02)


class HasSignalTest(unittest.TestCase):
    """Gate de RMS: o headset sem fio entrega zeros e nenhuma API acusa erro."""

    def setUp(self):
        mic = getattr(_audio, "Mic", None)
        self.has_signal = getattr(mic, "has_signal", None) if mic else None
        if not callable(self.has_signal):
            self.skipTest("wispr.audio.Mic.has_signal indisponivel")

    def test_digital_silence_is_rejected(self):
        self.assertFalse(self.has_signal(np.zeros(48000, dtype=np.float32)))

    def test_empty_buffer_is_rejected(self):
        self.assertFalse(self.has_signal(np.zeros(0, dtype=np.float32)))

    def test_normal_speech_level_is_accepted(self):
        self.assertTrue(self.has_signal(tone(300, 0.5, amp=0.1)))

    def test_very_quiet_but_real_room_noise_is_accepted(self):
        rng = np.random.default_rng(7)
        noise = (rng.standard_normal(48000) * 0.002).astype(np.float32)
        self.assertTrue(self.has_signal(noise))

    def test_a_signal_below_the_gate_is_rejected(self):
        self.assertFalse(self.has_signal(tone(300, 0.5, amp=1e-6)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
