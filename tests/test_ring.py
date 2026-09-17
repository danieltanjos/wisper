# -*- coding: utf-8 -*-
"""Testes da aritmetica do ring buffer do microfone.

NENHUM Mic real e construido aqui: `Mic.__init__` cobra COM, abre um stream
WASAPI e sobe uma thread supervisora. Esta suite tem que ser segura de rodar com
a maquina em uso, entao o Mic de verdade e criado com `object.__new__` -- sem
`__init__`, sem stream, sem device -- e so a matematica e exercitada:
`_cb()` (escrita do callback), `mark()` (pre-roll) e `take()` (recorte).

Os mesmos casos rodam duas vezes, pela classe `_RingCases`:

* contra `FakeRing`, uma replica das contas que serve de especificacao
  executavel e continua passando mesmo sem `wispr/audio.py` instalado;
* contra o `Mic` de verdade, que e quem precisa estar certo.

Semantica (docs/ARCHITECTURE.md secao 4 e o proprio wispr/audio.py):
    mark()      -> `written` rebobinado pelo pre-roll, piso em 0. NAO e' preso
                   no frame mais velho vivo: quem detecta a perda e' o take().
    take(start) -> (audio 48k, 'ok' | 'lapped' | 'empty')
                   'lapped' quando o pedido passou da capacidade UTIL do ring;
                   'empty' quando nao ha frames NOVOS **ou** quando o gate de
                   RMS reprovou o audio (silencio digital do headset sem fio).

Capacidade util, e nao `ring.size`: o `take()` guarda uma faixa de um bloco
(`room = size - block`) porque o callback continua escrevendo enquanto a copia
acontece, e o frame mais velho de uma leitura cheia e' exatamente a celula que o
escritor esta sobrescrevendo agora. A guarda so liga quando o ring tem mais de
quatro blocos -- e em producao ela esta SEMPRE ligada, porque o `Mic.__init__`
limita `block` a `ring.size // 8`. Dai as duas familias de casos aqui:
`_RingCases` com a guarda desligada (a aritmetica limpa de pre-roll e virada) e
`_GuardBandCases` com os numeros de producao.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.dirname(_HERE), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import _safety  # noqa: E402  stubs antes de qualquer import de wispr

import threading
import unittest

_NO_NUMPY = not _safety.has_module("numpy")
if not _NO_NUMPY:
    import numpy as np

_audio, _why = _safety.load_pure("wispr.audio")


def setUpModule():
    if _NO_NUMPY:
        raise unittest.SkipTest("numpy nao instalado; rode scripts\\setup.ps1")


def ramp(first, count):
    """Frames com valor igual ao proprio indice absoluto: confere de bater o olho."""
    return np.arange(first, first + count, dtype=np.float32)


def _rms(x):
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x.astype(np.float64)))))


# --------------------------------------------------------------------------- #
# replica
# --------------------------------------------------------------------------- #

class FakeRing:
    """Replica fiel das contas de `Mic`, sem nada de audio por perto.

    `written` e um contador absoluto que nunca volta; o indice fisico e
    `written % size`. Trabalhar em indice absoluto e o que faz a deteccao de
    volta virar uma comparacao em vez de malabarismo de ponteiros.
    """

    def __init__(self, ring_frames=1000, sr=1000, preroll_sec=0.1, silence_rms=3e-5,
                 block=250):
        self.ring = np.zeros(int(ring_frames), dtype=np.float32)
        self.sr = int(sr)
        self.block = int(block)
        self.preroll_sec = float(preroll_sec)
        self.silence_rms = float(silence_rms)
        self.written = 0
        self.events = []
        self.cb_err = ""
        self._lock = threading.Lock()

    # escrita do callback, incluindo a virada
    def write(self, block):
        blk = np.asarray(block, dtype=np.float32).reshape(-1)
        frames = blk.size
        size = self.ring.size
        with self._lock:
            if frames >= size:
                # Ring minusculo para o bloco: fica so o final, mas o alinhamento
                # written % size tem que ser mantido, senao o _slice desanda.
                tail = blk[frames - size:]
                e = (self.written + frames) % size
                self.ring[e:] = tail[:size - e]
                self.ring[:e] = tail[size - e:]
                self.written += frames
                return
            p = self.written % size
            if p + frames <= size:
                self.ring[p:p + frames] = blk
            else:
                k = size - p
                self.ring[p:] = blk[:k]
                self.ring[:frames - k] = blk[k:]
            self.written += frames   # publica DEPOIS que o dado entrou

    def mark(self):
        return max(0, self.written - int(self.sr * self.preroll_sec))

    def _slice(self, end, n):
        size = self.ring.size
        s = (end - n) % size
        if s + n <= size:
            return self.ring[s:s + n].copy()
        return np.concatenate([self.ring[s:], self.ring[:(s + n) - size]])

    def room(self):
        """Capacidade util: o ring menos a faixa de guarda de um bloco.

        A guarda so existe quando ha mais de quatro blocos no ring; abaixo disso
        ela comeria a gravacao inteira e o remedio seria pior que a doenca.
        """
        size = self.ring.size
        return size - self.block if size > 4 * self.block else size

    def take(self, start):
        end = self.written
        n = int(min(end - start, self.room()))
        if n <= 0:
            return np.zeros(0, np.float32), "empty"
        out = self._slice(end, n)
        if not (out.size and _rms(out) > self.silence_rms):
            # O Mic de verdade limita o AVISO a um por segundo (nunca o gate, que e
            # obrigatorio por gravacao). A replica nao modela relogio: cada caso aqui
            # usa um objeto novo e chama take() uma vez, entao da no mesmo.
            self.events.append("silent_input")
            return np.zeros(0, np.float32), "empty"
        # Compara com `n`, nao com o tamanho do ring: pedir mais do que a
        # capacidade UTIL ja e' perda de inicio, mesmo que caiba no array.
        if (end - start) > n:
            self.events.append("lapped")
            return out, "lapped"
        return out, "ok"


class _MicHarness:
    """O Mic de verdade, sem `__init__`, alimentado na mao.

    So os atributos que `_cb`, `mark` e `take` realmente leem sao plantados. Se
    algum dia faltar um, o teste quebra com AttributeError apontando o nome --
    que e exatamente o aviso que a gente quer.
    """

    def __init__(self, mic_cls, ring_frames=1000, sr=1000, preroll_sec=0.1, block=250):
        from collections import deque

        mic = object.__new__(mic_cls)          # nunca chama __init__: nada e aberto
        mic.ring = np.zeros(int(ring_frames), dtype=np.float32)
        mic.written = 0
        mic.sr = int(sr)
        mic.ring_sec = ring_frames / float(sr)
        # `block` plantado de proposito: o take() le ele com getattr(..., BLOCK) e,
        # sem isso, o teste rodaria com o BLOCK de 480 do modulo enquanto o FakeRing
        # usava outro numero -- a faixa de guarda ligaria de um lado so e a
        # divergencia passaria despercebida ate mudar o tamanho do ring de teste.
        mic.block = int(block)
        mic.preroll_sec = float(preroll_sec)
        mic.silence_rms = 3e-5
        mic.xruns = deque(maxlen=64)
        mic.last_audio = np.zeros(0, np.float32)
        mic.last_rms = 0.0
        mic.device = -1
        mic.name = "fake"
        mic._cb_err = ""
        mic._silent_t = 0.0
        self.events = []
        mic.on_event = lambda name, **kw: self.events.append(name)
        self.mic = mic

    @property
    def ring(self):
        return self.mic.ring

    @property
    def written(self):
        return self.mic.written

    @property
    def cb_err(self):
        return self.mic._cb_err

    def write(self, block):
        blk = np.asarray(block, dtype=np.float32).reshape(-1, 1)   # PortAudio da 2-D
        self.mic._cb(blk, blk.shape[0], None, None)

    def mark(self):
        return self.mic.mark()

    def take(self, start):
        return self.mic.take(start)


# --------------------------------------------------------------------------- #
# casos, rodados contra os dois
# --------------------------------------------------------------------------- #

class _RingCases:

    RING = 1000
    SR = 1000          # sr redondo para o pre-roll cair em numero inteiro exato
    PREROLL_SEC = 0.1  # -> 100 frames
    PREROLL = 100
    # 1000 nao chega a ser 4 blocos de 250, entao a faixa de guarda fica DESLIGADA
    # nesta familia e a capacidade util e o ring inteiro. E o que mantem as contas
    # de pre-roll e de virada legiveis; a guarda tem _GuardBandCases so para ela.
    BLOCK = 250

    def new(self):
        raise NotImplementedError

    def setUp(self):
        self.r = self.new()

    def fill(self, count):
        self.r.write(ramp(self.r.written, count))
        self.assertEqual(self.r.cb_err, "", "o callback engoliu uma excecao")

    # --- pre-roll ---------------------------------------------------------- #

    def test_mark_rewinds_by_the_preroll(self):
        """Sem o recuo a primeira silaba some entre a tecla e o mark()."""
        self.fill(500)
        self.assertEqual(self.r.mark(), 400)

    def test_mark_never_goes_negative_on_a_cold_start(self):
        self.fill(50)
        self.assertEqual(self.r.mark(), 0)

    def test_preroll_content_really_comes_back(self):
        self.fill(500)
        start = self.r.mark()
        self.fill(300)
        audio, status = self.r.take(start)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(400, 400))

    def test_mark_is_allowed_to_point_before_the_oldest_frame(self):
        """mark() nao prende no ring; quem acusa a perda e o take() com 'lapped'."""
        self.fill(5000)
        self.assertEqual(self.r.mark(), 4900)

    # --- leitura normal ---------------------------------------------------- #

    def test_take_returns_exactly_the_frames_since_mark(self):
        self.fill(200)
        start = self.r.mark()
        self.fill(300)
        audio, status = self.r.take(start)
        self.assertEqual(status, "ok")
        self.assertEqual(len(audio), self.r.written - start)

    def test_take_twice_from_the_same_mark_is_stable(self):
        self.fill(400)
        start = self.r.mark()
        self.fill(100)
        a, sa = self.r.take(start)
        b, sb = self.r.take(start)
        self.assertEqual((sa, sb), ("ok", "ok"))
        np.testing.assert_array_equal(a, b)

    def test_dtype_stays_float32(self):
        self.fill(300)
        audio, _ = self.r.take(self.r.mark())
        self.assertEqual(audio.dtype, np.float32)

    def test_take_does_not_alias_the_ring(self):
        """O worker mexe no array devolvido enquanto o callback continua escrevendo."""
        self.fill(300)
        audio, _ = self.r.take(0)
        audio[0] = -12345.0
        again, _ = self.r.take(0)
        self.assertNotEqual(float(again[0]), -12345.0)

    def test_many_small_blocks_add_up(self):
        for _ in range(10):
            self.fill(48)
        self.assertEqual(self.r.written, 480)
        audio, status = self.r.take(0)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(0, 480))

    # --- vazio ------------------------------------------------------------- #

    def test_empty_on_a_completely_cold_ring(self):
        audio, status = self.r.take(0)
        self.assertEqual(status, "empty")
        self.assertEqual(len(audio), 0)

    def test_empty_when_nothing_arrived_after_the_mark(self):
        self.fill(300)
        audio, status = self.r.take(self.r.written)
        self.assertEqual(status, "empty")
        self.assertEqual(len(audio), 0)

    def test_start_in_the_future_is_empty_not_negative(self):
        self.fill(100)
        audio, status = self.r.take(99999)
        self.assertEqual(status, "empty")
        self.assertEqual(len(audio), 0)

    def test_digital_silence_is_reported_as_empty(self):
        """Headset sem fio dormindo entrega zeros com tudo 'Active'. So o RMS pega."""
        self.r.write(np.zeros(500, dtype=np.float32))
        audio, status = self.r.take(0)
        self.assertEqual(status, "empty")
        self.assertEqual(len(audio), 0, "audio mudo nao pode vazar pelo tamanho")
        self.assertIn("silent_input", self.r.events)

    # --- virada do buffer -------------------------------------------------- #

    def test_forced_wrap_returns_contiguous_audio(self):
        """A regiao pedida cruza o fim fisico do ring: 850..999 e depois 0..249."""
        self.fill(1950)
        start = self.r.mark()
        self.assertEqual(start, 1850)
        self.fill(300)
        audio, status = self.r.take(start)
        self.assertEqual(status, "ok")
        self.assertEqual(len(audio), 400)
        np.testing.assert_array_equal(audio, ramp(1850, 400))

    def test_wrap_exactly_on_the_boundary(self):
        self.fill(1000)          # indice fisico volta a 0
        start = self.r.mark()
        self.fill(200)
        audio, status = self.r.take(start)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(900, 300))

    def test_a_single_block_that_wraps(self):
        self.fill(900)
        self.fill(300)           # esse bloco sozinho cruza a fronteira
        audio, status = self.r.take(1000)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(1000, 200))

    def test_many_wraps_in_a_row_stay_aligned(self):
        for _ in range(37):
            self.fill(137)       # 5069 frames, nenhum multiplo do ring
        audio, status = self.r.take(self.r.mark())
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(self.r.written - 100, 100))

    def test_block_bigger_than_the_ring_keeps_the_tail_aligned(self):
        self.r.write(ramp(0, 2500))
        audio, status = self.r.take(0)
        self.assertEqual(status, "lapped")
        np.testing.assert_array_equal(audio, ramp(1500, 1000))

    # --- lapped ------------------------------------------------------------ #

    def test_lapped_when_the_writer_overtakes_the_mark(self):
        """Ditado mais longo que o ring: o comeco se perdeu, e o app tem que saber."""
        self.fill(500)
        start = self.r.mark()          # 400
        self.fill(1300)                # written 1800; so 800..1799 sobreviveram
        audio, status = self.r.take(start)
        self.assertEqual(status, "lapped")
        self.assertEqual(len(audio), self.RING)
        np.testing.assert_array_equal(audio, ramp(800, 1000))
        self.assertIn("lapped", self.r.events)

    def test_exactly_full_is_still_ok_not_lapped(self):
        self.fill(1000)
        audio, status = self.r.take(0)
        self.assertEqual(status, "ok")
        self.assertEqual(len(audio), 1000)

    def test_one_frame_past_full_is_lapped(self):
        self.fill(1001)
        audio, status = self.r.take(0)
        self.assertEqual(status, "lapped")
        self.assertEqual(len(audio), 1000)

    def test_lapped_still_returns_usable_audio(self):
        self.fill(5000)
        audio, status = self.r.take(0)
        self.assertEqual(status, "lapped")
        np.testing.assert_array_equal(audio, ramp(4000, 1000))

    def test_status_is_always_one_of_the_three(self):
        for written, start in ((0, 0), (100, 0), (100, 100), (5000, 0), (5000, 4999)):
            with self.subTest(written=written, start=start):
                r = self.new()
                if written:
                    r.write(ramp(0, written))
                _, status = r.take(start)
                self.assertIn(status, ("ok", "lapped", "empty"))


class _FakeMixin:
    """A replica: especificacao executavel, roda ate sem o wispr/audio.py."""

    def new(self):
        return FakeRing(ring_frames=self.RING, sr=self.SR,
                        preroll_sec=self.PREROLL_SEC, block=self.BLOCK)


class _RealMixin:
    """O Mic de producao, criado sem __init__ e alimentado pelo proprio _cb()."""

    def setUp(self):
        if _audio is None:
            self.skipTest(_why or "wispr.audio indisponivel")
        self.mic_cls = getattr(_audio, "Mic", None)
        if self.mic_cls is None:
            self.skipTest("wispr.audio.Mic indisponivel")
        for name in ("_cb", "mark", "take", "_slice"):
            if not callable(getattr(self.mic_cls, name, None)):
                self.skipTest("Mic.%s() nao existe; a conta do ring nao esta "
                              "alcancavel sem abrir stream" % name)
        super().setUp()

    def new(self):
        return _MicHarness(self.mic_cls, ring_frames=self.RING, sr=self.SR,
                           preroll_sec=self.PREROLL_SEC, block=self.BLOCK)


class FakeRingTest(_FakeMixin, _RingCases, unittest.TestCase):
    pass


class RealMicRingTest(_RealMixin, _RingCases, unittest.TestCase):

    def test_never_opened_a_stream(self):
        """Cinto e suspensorio: o harness nao pode ter criado stream nenhum."""
        self.assertFalse(hasattr(self.r.mic, "stream") and self.r.mic.stream,
                         "o Mic de teste ficou com um stream pendurado")

    def test_xruns_from_the_driver_are_recorded_not_raised(self):
        blk = ramp(0, 100).reshape(-1, 1)
        self.r.mic._cb(blk, 100, None, "input overflow")
        self.assertEqual(self.r.cb_err, "")
        self.assertEqual(self.r.written, 100)
        self.assertEqual(len(self.r.mic.xruns), 1)


class _GuardBandCases:
    """A configuracao de PRODUCAO: ring grande, guarda ligada.

    `Mic.__init__` limita `block` a `ring.size // 8`, entao no app de verdade
    `size > 4 * block` e sempre verdade e o `take()` sempre devolve no maximo
    `size - block` frames. A faixa que ele abre mantida e a do frame MAIS VELHO,
    que e' justamente a celula que o callback esta sobrescrevendo enquanto a
    copia acontece — sem ela o inicio do ditado sai picotado, sem erro nenhum.
    """

    RING = 4000
    SR = 1000
    PREROLL_SEC = 0.1
    BLOCK = 480        # 4000 > 4*480: guarda LIGADA, capacidade util = 3520

    def new(self):
        raise NotImplementedError

    def setUp(self):
        self.r = self.new()
        self.room = self.RING - self.BLOCK

    def tearDown(self):
        # `_cb` engole excecao de proposito (nao pode derrubar o stream), entao o
        # unico jeito de saber que ela aconteceu e' olhar aqui.
        self.assertEqual(self.r.cb_err, "", "o callback engoliu uma excecao")

    def test_below_the_guard_band_is_plain_ok(self):
        self.r.write(ramp(0, self.room))
        audio, status = self.r.take(0)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(0, self.room))

    def test_one_frame_past_the_guard_band_is_lapped(self):
        self.r.write(ramp(0, self.room + 1))
        audio, status = self.r.take(0)
        self.assertEqual(status, "lapped")
        self.assertEqual(len(audio), self.room)
        np.testing.assert_array_equal(audio, ramp(1, self.room))

    def test_a_full_ring_drops_the_oldest_block_not_the_newest(self):
        """O audio recente e' o que importa; o que se perde e o comeco."""
        self.r.write(ramp(0, self.RING))
        audio, status = self.r.take(0)
        self.assertEqual(status, "lapped")
        self.assertEqual(len(audio), self.room)
        np.testing.assert_array_equal(audio, ramp(self.BLOCK, self.room))

    def test_the_newest_frame_is_always_the_last_one_returned(self):
        self.r.write(ramp(0, 9000))
        audio, _ = self.r.take(0)
        self.assertEqual(float(audio[-1]), 8999.0)

    def test_a_short_take_is_untouched_by_the_guard(self):
        self.r.write(ramp(0, 5000))
        audio, status = self.r.take(4800)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(4800, 200))

    def test_preroll_still_works_with_the_guard_on(self):
        self.r.write(ramp(0, 2000))
        start = self.r.mark()
        self.assertEqual(start, 1900)
        self.r.write(ramp(2000, 300))
        audio, status = self.r.take(start)
        self.assertEqual(status, "ok")
        np.testing.assert_array_equal(audio, ramp(1900, 400))


class FakeRingGuardBandTest(_FakeMixin, _GuardBandCases, unittest.TestCase):
    pass


class RealMicGuardBandTest(_RealMixin, _GuardBandCases, unittest.TestCase):
    pass


class LastAudioTest(unittest.TestCase):
    """Mesmo recusando por silencio, o audio cru fica guardado para diagnostico."""

    def setUp(self):
        if _audio is None or getattr(_audio, "Mic", None) is None:
            self.skipTest(_why or "wispr.audio.Mic indisponivel")
        self.h = _MicHarness(_audio.Mic)

    def test_silent_take_still_keeps_last_audio_for_the_log(self):
        self.h.write(np.zeros(500, dtype=np.float32))
        audio, status = self.h.take(0)
        self.assertEqual(status, "empty")
        self.assertEqual(len(audio), 0)
        self.assertEqual(float(self.h.mic.last_rms), 0.0)
        # O array devolvido vem vazio para ninguem pular o gate olhando o tamanho,
        # mas o audio cru tem que sobrar em last_audio, senao nao da para provar
        # depois que o headset entregou zeros (nenhuma API do Windows acusa isso).
        self.assertEqual(len(self.h.mic.last_audio), 500)
        self.assertEqual(float(np.max(np.abs(self.h.mic.last_audio))), 0.0)

    def test_good_take_records_the_rms(self):
        self.h.write(ramp(1, 500) / 1000.0)
        audio, status = self.h.take(0)
        self.assertEqual(status, "ok")
        self.assertGreater(float(self.h.mic.last_rms), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
