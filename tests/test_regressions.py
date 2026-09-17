# -*- coding: utf-8 -*-
"""Regressoes das falhas apontadas pelas auditorias.

Nenhuma delas era pega pela suite que existia, e todas as cinco sao invisiveis
em producao: o app roda sob pythonw.exe, sem console, e quatro das cinco falham
em SILENCIO (a excecao no on_stop deixava a maquina presa em 'transcribing' com
a bandeja jurando que estava trabalhando).

O que esta coberto aqui, e o sintoma que cada teste impede de voltar:

1. `Mic._emit` -- o payload de varios eventos carrega o friendly name do
   endpoint em `name=`, e o parametro do proprio `_emit` se chama `name`. Com um
   parametro comum o Python recusa a chamada inteira, na abertura do microfone e
   no detector de silencio digital (ARCHITECTURE.md secao 4).
2. Tamanho do ring -- `ring_sec` menor que `max_record_sec` faz o ditado longo
   perder o comeco sem erro nenhum, e `_num()` voltando ao DEFAULT em vez de
   prender no limite fazia `ring_sec: 300` virar 180 caladamente.
3. `inject.Delivery` e' subclasse de `str`, nao dict: um `isinstance(res, dict)`
   no app.py matava de uma vez as tres redes de seguranca pos-entrega -- entre
   elas a UNICA evidencia de que o texto chegou, ja que sob UIPI o SendInput e o
   Ctrl+V somem com GetLastError() == 0 (ARCHITECTURE.md secao 5).
4. Excecao dentro do `on_stop` depois de o estado ja ter virado 'transcribing':
   o app aceitava o Win+A seguinte so para responder "ainda processando".
5. `Esc` durante a transcricao: o gate do cancelamento e' `cancellable`, que
   sobrevive ao Enter de proposito. Gatear no `recording` fazia o Esc fechar o
   dialogo do aplicativo em foco E a transcricao ser injetada logo depois.

A segunda rodada (re-auditoria adversarial) acrescentou sete, todas
sobreviventes de uma rodada de correcao anterior e todas igualmente silenciosas:

6. `blocked` x `target_elevated` no `app.py`. Rodar o wisper elevado e' a
   solucao RECOMENDADA para o hook sobreviver a janelas de administrador
   (ARCHITECTURE.md secao 3), e ai `target_elevated` e' True o tempo todo. Agir
   na resposta crua fazia o texto entrar na janela E ir para o clipboard: o
   Ctrl+V seguinte do usuario escrevia tudo pela segunda vez.
7. Envio parcial no `inject.py`: com `.sent` contando so o bloco que falhou, "o
   bloco 1 entrou e o 2 foi descartado" era indistinguivel de "nada entrou". O
   `deliver()` redigitava o texto inteiro e o `app.py` ainda colava por cima --
   os 64 primeiros caracteres tres vezes na janela do usuario.
8. Transcricao vazia (um U+200B sozinho) tratada como falha de entrega: ela
   sobrescrevia a area de transferencia do usuario com uma string VAZIA e ainda
   anunciava um erro que nao aconteceu.
9. `App._mic_alive`: um `Mic` sem o atributo `stream` prendia `mic_ok` em False
   para sempre (todo Win+A respondia "Microfone indisponivel"), e ler
   `stream.active` daqui e' `Pa_IsStreamActive` num ponteiro que o supervisor
   pode estar liberando -- crash de processo que nenhum `except` pega.
10. Job cancelado antigo escondendo a pilula da sessao NOVA: o usuario ficava
    falando sem nenhum retorno na tela ate' o proximo Enter.
11. `Tray._effective_state` so subia para vermelho. O watchdog reinstalava o
    hook segundos depois e o icone continuava vermelho ate' reiniciar o app,
    porque `App.on_hook_error()` nao arma timer de volta ao verde de proposito.
12. Fim de repaste sem limpar `hotkey.cancellable`: o hook passava a engolir o
    Esc do Windows INTEIRO, sem ditado nenhum, ate' comer um Esc do usuario.

Nada aqui abre microfone, carrega modelo, instala hook, injeta tecla, le
clipboard ou abre janela: o `Mic` nasce de `object.__new__`, o `App` roda com
bandeja, pilula, microfone e motor de mentira, a bandeja e' construida sem
nunca chamar `run()` (pystray e Pillow sao stubs), o `_send_blob` do inject e'
um duble que so conta eventos, e o `_safety` transforma as chamadas Win32
perigosas em bombas.
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
import re
import threading
import types
import unittest
from datetime import datetime
from unittest import mock

_NO_NUMPY = not _safety.has_module("numpy")
if not _NO_NUMPY:
    import numpy as np

_config, _why_config = _safety.load_pure("wispr.config")
_audio, _why_audio = _safety.load_pure("wispr.audio")
_inject, _why_inject = _safety.load_pure("wispr.inject")
_hotkey, _why_hotkey = _safety.load_pure("wispr.hotkey")
_app, _why_app = _safety.load_pure("wispr.app")
_tray, _why_tray = _safety.load_pure("wispr.tray")


def setUpModule():
    if _NO_NUMPY:
        raise unittest.SkipTest("numpy nao instalado; rode scripts\\setup.ps1")


# --------------------------------------------------------------------------- #
# 1. colisao de kwarg no _emit
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_audio, _why_audio or "wispr.audio indisponivel")
class EmitKwargTest(unittest.TestCase):
    """`_emit(evento, name=<endpoint>)` tem que funcionar para TODO evento."""

    def _mic(self):
        """Mic sem `__init__`: nada de COM, nada de stream, nada de supervisor."""
        mic = object.__new__(_audio.Mic)
        seen = []
        # A assinatura documentada do handler e' exatamente esta: o nome do EVENTO
        # vem posicional e o resto por keyword. E' ela que colide.
        mic.on_event = lambda name, **kw: seen.append((name, kw))
        mic.device = 7
        mic.name = "Microfone (Logitech PRO X)"
        mic.hostapi = "Windows WASAPI"
        mic.sr = 48000
        mic.fallback = False
        mic.reopens = 0
        return mic, seen

    def _event_names(self):
        """Todo nome de evento que existe hoje em wispr/audio.py.

        Lido do fonte, e nao de uma lista escrita aqui: um evento novo entra no
        teste sozinho, que e' o ponto -- a colisao voltaria justamente no proximo
        evento que alguem adicionasse carregando `name=` no payload.
        """
        names = set(getattr(_audio, "_EVENT_LEVEL", {}))
        names.update(re.findall(r'_emit\(\s*"([a-z_]+)"', inspect.getsource(_audio)))
        return sorted(names)

    def test_the_source_scan_really_found_the_events(self):
        # Sem esta ancora, um `_emit` renomeado faria o teste abaixo iterar sobre
        # uma lista vazia e passar sem exercitar absolutamente nada.
        names = self._event_names()
        for expected in ("open", "stream_dead", "silent_input", "lapped"):
            self.assertIn(expected, names)

    def test_the_event_name_is_positional_only(self):
        mic, _ = self._mic()
        first = list(inspect.signature(mic._emit).parameters.values())[0]
        self.assertEqual(first.name, "name")
        self.assertIs(first.kind, inspect.Parameter.POSITIONAL_ONLY)

    def test_every_event_can_carry_a_name_payload(self):
        for ev in self._event_names():
            with self.subTest(event=ev):
                mic, seen = self._mic()
                mic._emit(ev, name="Microfone (Logitech PRO X)", extra=1)
                # Chamado de verdade: a colisao antiga nem chegava no handler.
                self.assertEqual(len(seen), 1)
                got, kw = seen[0]
                self.assertEqual(got, ev)
                self.assertNotIn("name", kw)     # 'name' e' o evento, nunca o endpoint
                self.assertEqual(kw["device_name"], "Microfone (Logitech PRO X)")
                self.assertEqual(kw["extra"], 1)

    def test_emit_open_carries_the_endpoint_name(self):
        # `open` sai em TODA abertura de microfone, inclusive na primeira: era o
        # caminho mais quente dos que quebravam.
        mic, seen = self._mic()
        mic._emit_open()
        self.assertEqual(len(seen), 1)
        ev, kw = seen[0]
        self.assertEqual(ev, "open")
        self.assertEqual(kw["device_name"], "Microfone (Logitech PRO X)")
        self.assertEqual(kw["device"], 7)
        self.assertEqual(kw["samplerate"], 48000)

    def test_a_handler_that_raises_never_escapes(self):
        # E' este `except` que tornava a colisao invisivel: sem console, uma
        # excecao no handler so existe no log.
        mic, _ = self._mic()
        mic.on_event = lambda *a, **k: 1 / 0
        mic._emit("stream_dead", name="X", err="boom")

    @unittest.skipUnless(_app, _why_app or "wispr.app indisponivel")
    def test_the_app_handler_accepts_the_renamed_key(self):
        # Quem consome de verdade e' o app: `device_name` nao pode explodir la.
        app = _make_app()
        app._on_mic_event("open", device_name="Microfone (Logitech PRO X)", device=7)
        app._on_mic_event("silent_input", device_name="X", rms=0.0, seconds=3.0)


# --------------------------------------------------------------------------- #
# 2. tamanho do ring e clamp do _num
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_config, _why_config or "wispr.config indisponivel")
class RingSizingTest(unittest.TestCase):
    """O ring e' o teto REAL de uma fala; `max_record_sec` tem que caber nele."""

    def test_ring_sec_is_at_least_max_record_sec(self):
        d = _config.DEFAULTS
        self.assertGreaterEqual(float(d["ring_sec"]), float(d["max_record_sec"]))

    def test_the_guard_band_and_the_preroll_still_fit(self):
        # O `take()` reserva um bloco de faixa de guarda e o `mark()` rebobina o
        # pre-roll: o teto util e' menor que `ring_sec`, e e' ELE que precisa
        # aguentar uma gravacao inteira de `max_record_sec`.
        d = _config.DEFAULTS
        sr = int(d["capture_sr"])
        util = int(sr * float(d["ring_sec"])) - int(d["block"])
        needed = (float(d["max_record_sec"]) + float(d["preroll_sec"])) * sr
        self.assertGreaterEqual(util, needed)

    @unittest.skipUnless(_audio, _why_audio or "wispr.audio indisponivel")
    def test_the_module_fallback_agrees_with_the_defaults(self):
        # `wispr/audio.py` tem o proprio RING_SEC para quando a chave falta no
        # cfg. Os dois numeros divergindo dariam dois tetos de gravacao
        # diferentes conforme o config.json existisse ou nao.
        self.assertEqual(float(_audio.RING_SEC), float(_config.DEFAULTS["ring_sec"]))

    @unittest.skipUnless(_audio, _why_audio or "wispr.audio indisponivel")
    def test_the_default_itself_survives_the_clamp(self):
        # Um teto de sanidade menor que o proprio default cortaria o ring de
        # fabrica, e ninguem nunca veria por que a fala longa perdia o comeco.
        self.assertLessEqual(float(_config.DEFAULTS["ring_sec"]), _audio.MAX_RING_SEC)
        got = _audio._num(_config.DEFAULTS, "ring_sec", _audio.RING_SEC, float,
                          minimum=0.5, maximum=_audio.MAX_RING_SEC)
        self.assertEqual(got, float(_config.DEFAULTS["ring_sec"]))


@unittest.skipUnless(_audio, _why_audio or "wispr.audio indisponivel")
class NumClampTest(unittest.TestCase):
    """`_num()` PRENDE no limite. Voltar ao default e' pior que o erro de quem
    editou o config.json: `ring_sec: 300` virava 180 em silencio e a pessoa
    continuava perdendo o comeco das falas longas sem entender por que."""

    def setUp(self):
        # O aviso sai uma vez por chave; zerar aqui mantem os casos independentes.
        _audio._CLAMPED.difference_update({"ring_sec", "block", "capture_sr"})

    def test_above_the_maximum_is_clamped_not_defaulted(self):
        got = _audio._num({"ring_sec": 100000.0}, "ring_sec", 180.0, float,
                          minimum=0.5, maximum=_audio.MAX_RING_SEC)
        self.assertEqual(got, _audio.MAX_RING_SEC)
        self.assertNotEqual(got, 180.0)

    def test_below_the_minimum_is_clamped_not_defaulted(self):
        got = _audio._num({"ring_sec": 0.001}, "ring_sec", 180.0, float,
                          minimum=0.5, maximum=_audio.MAX_RING_SEC)
        self.assertEqual(got, 0.5)

    def test_the_clamp_warns_once_per_key(self):
        _audio._num({"ring_sec": 100000.0}, "ring_sec", 180.0, float, maximum=300.0)
        self.assertIn("ring_sec", _audio._CLAMPED)

    def test_the_bound_comes_back_in_the_requested_type(self):
        # `block` vai para o np.zeros e para o blocksize do PortAudio: 48000.0 no
        # lugar de 48000 quebraria os dois.
        got = _audio._num({"block": 10 ** 9}, "block", 480, int, minimum=32, maximum=48000)
        self.assertEqual(got, 48000)
        self.assertIsInstance(got, int)

    def test_garbage_still_falls_back_to_the_default(self):
        # Prender so vale para numero fora da faixa: texto, None, NaN e Inf nao
        # tem limite para onde ir, e a captura nao pode cair por causa deles.
        for value in ("abc", None, float("nan"), float("inf"), [1], 10 ** 400):
            with self.subTest(value=value):
                got = _audio._num({"ring_sec": value}, "ring_sec", 180.0, float,
                                  minimum=0.5, maximum=300.0)
                self.assertEqual(got, 180.0)

    def test_a_missing_key_is_the_default(self):
        self.assertEqual(_audio._num({}, "ring_sec", 180.0, float, minimum=0.5,
                                     maximum=300.0), 180.0)

    def test_a_value_inside_the_range_is_untouched(self):
        self.assertEqual(_audio._num({"ring_sec": 240.0}, "ring_sec", 180.0, float,
                                     minimum=0.5, maximum=300.0), 240.0)


# --------------------------------------------------------------------------- #
# dubles do app: nenhum deles encosta em hardware
# --------------------------------------------------------------------------- #

class _FakeOverlay:
    """A pilula, sem Tk. Guarda o que teria sido mostrado."""

    def __init__(self):
        self.messages = []
        self.calls = []

    def start(self):
        return self

    def show_recording(self):
        self.calls.append("recording")

    def show_transcribing(self):
        self.calls.append("transcribing")

    def show_message(self, text, ms=1800):
        self.messages.append(text)

    def set_level(self, rms):
        pass

    def hide(self):
        self.calls.append("hide")

    def stop(self):
        pass


class _FakeTray:
    def __init__(self):
        self.states = []
        self.notices = []

    def run(self):
        pass

    def stop(self):
        pass

    def set_state(self, state):
        self.states.append(state)

    def notify(self, title, msg):
        self.notices.append((title, msg))


class _FakeHotkey:
    """So as duas flags publicas: e' delas que trata a regressao 5."""

    def __init__(self):
        self.recording = False
        self.cancellable = False
        self.installs = 1
        self.alive = True


class _FakeMic:
    """Microfone de mentira: sem PortAudio, sem COM, sem thread.

    Sem `level()`/`tail()` de proposito -- assim o `_level_loop` do app volta na
    hora em vez de ficar rodando durante o teste.
    """

    def __init__(self, audio=None, status="ok"):
        self.sr = 48000
        self.stream = types.SimpleNamespace(active=True)
        self.marks = 0
        self.status = status
        self.audio = audio if audio is not None else np.full(4800, 0.2, np.float32)

    def mark(self):
        self.marks += 1
        return 0

    def take(self, start):
        return self.audio, self.status


def _make_app(**over):
    """App de verdade com todas as bordas trocadas por dubles."""
    cfg = _config.Config(dict(_config.DEFAULTS))
    cfg["sounds"] = False            # winsound e' stub, mas nem o stub precisa entrar aqui
    cfg["keep_recordings"] = False   # nada de escrever wav dentro do projeto
    cfg.update(over)
    # `ensure_dirs` criaria pasta no projeto; a regra da suite e' nao escrever nele.
    with mock.patch.object(_config, "ensure_dirs", lambda: None):
        app = _app.App(cfg)
    app.overlay = _FakeOverlay()
    app.tray = _FakeTray()
    app.hotkey = _FakeHotkey()
    app.mic = _FakeMic()
    # Sem thread worker: o job fica na fila para o teste conferir, e nenhum
    # `_process` roda por conta propria com um motor de verdade.
    app._worker_ok = lambda: True
    return app


class _AppCase(unittest.TestCase):
    """Base que desarma todo timer que o app tiver armado."""

    def make_app(self, **over):
        app = _make_app(**over)
        self.addCleanup(self._disarm, app)
        return app

    @staticmethod
    def _disarm(app):
        try:
            app._disarm_timers()
        except Exception:
            pass
        timer, app._icon_timer = app._icon_timer, None
        if timer is not None:
            timer.cancel()


# --------------------------------------------------------------------------- #
# 3. duck-typing do Delivery
# --------------------------------------------------------------------------- #

class FakeDelivery(str):
    """Replica minima do `inject.Delivery`: subclasse de `str` com diagnostico.

    Esta aqui, e nao importada do inject, justamente para provar que o app le por
    ATRIBUTO e nao por tipo. Qualquer objeto que cumpra este contrato tem que
    chegar nas checagens pos-entrega.
    """

    def __new__(cls, mode, chars=0, target_elevated=None, lost_nontext_formats=False):
        self = str.__new__(cls, mode)
        self.mode = str(mode)
        self.chars = int(chars)
        self.target_elevated = target_elevated
        self.blocked = None
        self.lost_nontext_formats = bool(lost_nontext_formats)
        return self


@unittest.skipUnless(_app and _inject, _why_app or _why_inject or "modulos indisponiveis")
class DeliveryDuckTypingTest(_AppCase):
    """Sob UIPI o SendInput e o Ctrl+V somem com GetLastError() == 0: `chars` e' a
    unica prova de que o texto chegou (ARCHITECTURE.md secao 5)."""

    TEXT = "texto de teste com acentuacao: coracao, atencao"

    def deliver(self, result, elevated=False):
        """Roda `App._deliver` com o inject inteiro dublado."""
        app = self.make_app()
        saved = []
        calls = []

        def fake_deliver(text, cfg=None):
            calls.append(text)
            return result

        with mock.patch.object(_app, "foreground_is_elevated", lambda: elevated), \
                mock.patch.object(_inject, "deliver", fake_deliver), \
                mock.patch.object(_inject, "clip_set_text", saved.append):
            out = app._deliver(self.TEXT)
        return app, out, saved, calls

    def test_a_str_subclass_is_not_a_dict(self):
        # A raiz da falha: `isinstance(res, dict)` da False num Delivery e
        # desligava as tres redes de seguranca de uma vez.
        res = FakeDelivery("type", chars=0)
        self.assertIsInstance(res, str)
        self.assertNotIsInstance(res, dict)
        self.assertIsInstance(_inject.Delivery("type", chars=0), str)
        self.assertNotIsInstance(_inject.Delivery("type", chars=0), dict)

    def test_zero_chars_falls_back_to_the_clipboard(self):
        app, out, saved, _ = self.deliver(FakeDelivery("type", chars=0))
        self.assertEqual(out, ("clipboard", 0))
        self.assertEqual(saved, [self.TEXT])          # o texto nao se perde
        self.assertIn(_app.MSG_INJECT_FAIL, app.overlay.messages)
        self.assertEqual(app.tray.notices[-1][1], _app.MSG_INJECT_FAIL)

    def test_the_real_delivery_class_behaves_the_same(self):
        # Cinto e suspensorio: se o `Delivery` de verdade mudar de forma, o duble
        # acima continuaria passando sozinho.
        app, out, saved, _ = self.deliver(_inject.Delivery("paste", chars=0))
        self.assertEqual(out, ("clipboard", 0))
        self.assertEqual(saved, [self.TEXT])

    def test_a_good_delivery_is_reported_as_delivered(self):
        app, out, saved, calls = self.deliver(FakeDelivery("paste", chars=len(self.TEXT)))
        self.assertEqual(out, ("paste", len(self.TEXT)))
        self.assertEqual(saved, [])                   # nada de mexer no clipboard a toa
        self.assertEqual(calls, [self.TEXT])

    def test_an_elevated_target_reported_by_inject_falls_back(self):
        # A checagem por nivel de integridade do app passou; quem viu a janela
        # elevada foi o inject. Sem ler o atributo, o texto sumia calado.
        app, out, saved, _ = self.deliver(
            FakeDelivery("type", chars=len(self.TEXT), target_elevated=True))
        self.assertEqual(out, ("clipboard", 0))
        self.assertEqual(saved, [self.TEXT])
        self.assertIn(_app.MSG_ELEVATED_CLIP, app.overlay.messages)

    def test_a_lost_clipboard_image_is_announced(self):
        app, out, _, _ = self.deliver(
            FakeDelivery("paste", chars=len(self.TEXT), lost_nontext_formats=True))
        self.assertEqual(out, ("paste", len(self.TEXT)))
        self.assertIn(_app.MSG_CLIP_IMAGE, app.overlay.messages)

    def test_a_plain_string_still_works(self):
        # Contrato do CONTRACT.md: `deliver() -> str`. Sem diagnostico nenhum, o
        # melhor que se sabe e' que foi entregue o que se pediu.
        app, out, saved, _ = self.deliver("type")
        self.assertEqual(out, ("type", len(self.TEXT)))
        self.assertEqual(saved, [])

    def test_an_elevated_foreground_never_calls_inject(self):
        app, out, saved, calls = self.deliver(FakeDelivery("type", chars=99), elevated=True)
        self.assertEqual(out, ("clipboard", 0))
        self.assertEqual(calls, [])                   # nem tenta injetar
        self.assertEqual(saved, [self.TEXT])


# --------------------------------------------------------------------------- #
# 4. excecao dentro do on_stop
# --------------------------------------------------------------------------- #

def _boom(*a, **k):
    raise RuntimeError("falha sintetica dentro do on_stop")


@unittest.skipUnless(_app, _why_app or "wispr.app indisponivel")
class StateMachineRecoveryTest(_AppCase):
    """O `on_stop` move o estado para 'transcribing' ANTES de trabalhar. Uma
    excecao depois disso deixava a maquina presa para sempre: `on_start`
    respondia "ocupado", `on_stop` saia na porta e nenhum job era enfileirado."""

    def start(self, app):
        app.on_start()
        self.assertEqual(app.state, "recording")

    def test_a_crash_in_on_stop_does_not_raise(self):
        app = self.make_app()
        self.start(app)
        app._finish_recording = _boom
        app.on_stop()                                  # nao pode levantar

    def test_a_crash_in_on_stop_leaves_the_machine_idle(self):
        app = self.make_app()
        self.start(app)
        app._finish_recording = _boom
        app.on_stop()
        self.assertEqual(app.state, "idle")

    def test_a_crash_in_on_stop_releases_the_swallowed_keys(self):
        # Flag presa = tecla sumida no Windows inteiro. E' o pior defeito que o
        # app pode ter, e o caminho de excecao e' onde ela ficava.
        app = self.make_app()
        self.start(app)
        app.hotkey.cancellable = True
        app._finish_recording = _boom
        app.on_stop()
        self.assertFalse(app.hotkey.recording)
        self.assertFalse(app.hotkey.cancellable)

    def test_a_crash_in_on_stop_tells_the_user(self):
        app = self.make_app()
        self.start(app)
        app._finish_recording = _boom
        app.on_stop()
        self.assertIn(_app.MSG_STT_ERROR, app.overlay.messages)

    def test_the_next_dictation_still_goes_through(self):
        # A prova de que a maquina ficou recuperavel: um ditado INTEIRO depois do
        # acidente, ate' o job entrar na fila do worker.
        app = self.make_app()
        self.start(app)
        app._finish_recording = _boom
        app.on_stop()

        del app._finish_recording
        app.on_start()
        self.assertEqual(app.state, "recording")
        app.on_stop()
        self.assertEqual(app.state, "transcribing")
        self.assertFalse(app._jobs.empty())
        self.assertEqual(app._jobs.get_nowait()[0], app._session)

    def test_a_stop_without_a_recording_is_ignored(self):
        app = self.make_app()
        app.on_stop()
        self.assertEqual(app.state, "idle")
        self.assertTrue(app._jobs.empty())


# --------------------------------------------------------------------------- #
# 5. Esc durante a transcricao
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_hotkey and _config, _why_hotkey or "wispr.hotkey indisponivel")
class CancelDispatchTest(unittest.TestCase):
    """O worker do engine entrega o 'cancel' enquanto `cancellable` estiver
    ligada -- e ela sobrevive ao Enter de proposito."""

    def engine(self):
        fired = []
        # `HotkeyEngine.__init__` so parseia o chord; quem instala o hook e' o
        # `start()`, que nenhum teste chama.
        eng = _hotkey.HotkeyEngine(lambda: fired.append("start"),
                                   lambda: fired.append("stop"),
                                   lambda: fired.append("cancel"),
                                   cfg=_config.Config(dict(_config.DEFAULTS)))
        return eng, fired

    def drain(self, eng, events):
        """Roda o worker do engine ate' o fim, na propria thread do teste."""
        for ev in events:
            eng.q.put(ev)
        eng.q.put(None)
        eng._worker()

    def test_esc_fires_while_transcribing(self):
        # O estado exato do bug: o Enter ja passou (`recording` False) e a
        # transcricao esta em voo.
        eng, fired = self.engine()
        eng.recording = False
        eng.cancellable = True
        self.drain(eng, ["cancel"])
        self.assertEqual(fired, ["cancel"])

    def test_esc_is_ignored_when_there_is_no_dictation(self):
        eng, fired = self.engine()
        eng.recording = False
        eng.cancellable = False
        self.drain(eng, ["cancel"])
        self.assertEqual(fired, [])

    def test_enter_is_ignored_once_the_recording_ended(self):
        # O Enter e' gateado por `recording`, e so por ela: depois da gravacao ele
        # volta a ser do aplicativo em foco.
        eng, fired = self.engine()
        eng.recording = False
        eng.cancellable = True
        self.drain(eng, ["stop"])
        self.assertEqual(fired, [])

    def test_the_hook_gates_esc_on_cancellable_not_on_recording(self):
        # O hook proc nao pode ser chamado a mao (o lParam e' um ponteiro do SO),
        # entao a garantia fica no fonte: o Esc casa com `cancellable`.
        src = inspect.getsource(_hotkey.HotkeyEngine._hookproc)
        self.assertRegex(src, r"self\.cancellable and vk == self\._cancel_vk")
        self.assertRegex(src, r"self\.recording and vk == self\._stop_vk")


@unittest.skipUnless(_app and _inject, _why_app or _why_inject or "modulos indisponiveis")
class CancelSuppressesInjectionTest(_AppCase):
    """`Esc` volta para idle SEM INJETAR NADA, em qualquer estado (CONTRACT.md)."""

    AUDIO_S = 0.1

    def audio(self):
        return np.full(int(48000 * self.AUDIO_S), 0.2, np.float32)

    def test_enter_keeps_esc_alive_through_the_transcription(self):
        # Pre-condicao de tudo: o `on_stop` desliga `recording` e NAO pode
        # desligar `cancellable`, senao o Esc nem chega no app.
        app = self.make_app()
        app.on_start()
        app.hotkey.cancellable = True        # o engine liga ao entregar o 'start'
        app.on_stop()
        self.assertEqual(app.state, "transcribing")
        self.assertFalse(app.hotkey.recording)
        self.assertTrue(app.hotkey.cancellable)

    def test_cancel_while_transcribing_marks_the_session(self):
        app = self.make_app()
        app._state = "transcribing"
        app._session = 5
        app.hotkey.cancellable = True
        app.on_cancel()
        self.assertEqual(app._cancelled, 5)
        self.assertEqual(app.state, "idle")
        self.assertFalse(app.hotkey.cancellable)
        self.assertIn(_app.MSG_CANCELLED, app.overlay.messages)

    def test_the_job_in_flight_never_reaches_the_injection(self):
        app = self.make_app()
        app._state = "transcribing"
        app._session = 5
        app.hotkey.cancellable = True
        app.on_cancel()

        delivered = []
        app._deliver = lambda text: (delivered.append(text) or ("type", len(text)))
        app._get_engine = lambda: types.SimpleNamespace(
            backend="cpu", transcribe=lambda x: "texto que nao pode ser digitado")

        def bomb(*a, **k):
            raise AssertionError("inject.deliver foi chamado depois do Esc")

        with mock.patch.object(_inject, "deliver", bomb):
            app._process(5, self.audio(), "ok", datetime.now(), 1.0)

        self.assertEqual(delivered, [])
        self.assertEqual(app.state, "idle")
        self.assertEqual(app.last_text, "")          # nem entra no historico
        self.assertIn("hide", app.overlay.calls)
        self.assertFalse(app.hotkey.cancellable)     # o Esc volta a ser do app em foco

    def test_without_the_cancel_the_same_job_does_inject(self):
        # Controle: sem o Esc o mesmo caminho entrega o texto. Sem ele o teste
        # acima passaria ate' com o `_process` quebrado de outro jeito.
        app = self.make_app()
        app._state = "transcribing"
        app._session = 5

        delivered = []
        app._deliver = lambda text: (delivered.append(text) or ("type", len(text)))
        app._get_engine = lambda: types.SimpleNamespace(
            backend="cpu", transcribe=lambda x: "texto ditado")

        app._process(5, self.audio(), "ok", datetime.now(), 1.0)

        self.assertEqual(delivered, ["texto ditado"])
        self.assertEqual(app.last_text, "texto ditado")
        self.assertEqual(app.state, "idle")

    def test_cancel_while_recording_hides_the_pill(self):
        app = self.make_app()
        app.on_start()
        app.hotkey.cancellable = True
        app.on_cancel()
        self.assertEqual(app.state, "idle")
        self.assertIn("hide", app.overlay.calls)
        self.assertFalse(app.hotkey.recording)
        self.assertFalse(app.hotkey.cancellable)

    def test_cancel_with_nothing_running_only_clears_the_flags(self):
        # Flag presa: o Esc chegou sem ditado nenhum. Limpar e' o que devolve a
        # tecla ao aplicativo em foco.
        app = self.make_app()
        app.hotkey.cancellable = True
        app.on_cancel()
        self.assertEqual(app.state, "idle")
        self.assertFalse(app.hotkey.cancellable)
        self.assertEqual(app._cancelled, -1)


# --------------------------------------------------------------------------- #
# dubles e medidas comuns aos sete defeitos da re-auditoria
# --------------------------------------------------------------------------- #

def _units(s) -> int:
    """Code units UTF-16 do texto -- a unidade REAL do `Delivery.chars`.

    Nao e' `len()`: um emoji e' um code point so em Python e DUAS code units no
    SendInput. Medir com `len()` faria todo emoji parecer caractere faltando e
    transformaria uma entrega inteira em "parcial".
    """
    return len(str(s).encode("utf-16-le", "surrogatepass")) // 2


class _LegacyDelivery(str):
    """Resultado de um `inject` ANTIGO: sem `blocked` e sem `empty`.

    Existe para provar que a leitura por atributo degrada em vez de quebrar --
    sem a resposta acionavel, a crua (`target_elevated`) ainda vale.
    """

    def __new__(cls, mode, chars=0, target_elevated=None, lost_nontext_formats=False):
        self = str.__new__(cls, mode)
        self.mode = str(mode)
        self.chars = int(chars)
        self.target_elevated = target_elevated
        self.lost_nontext_formats = bool(lost_nontext_formats)
        return self


@unittest.skipUnless(_app and _inject, _why_app or _why_inject or "modulos indisponiveis")
class _DeliverCase(_AppCase):
    """Base dos testes de `App._deliver`: o `inject` inteiro dublado.

    Nenhum SendInput, nenhum clipboard: `inject.deliver` e `inject.clip_set_text`
    sao substituidos, e o que teria ido para a area de transferencia fica numa
    lista que o teste inspeciona.
    """

    TEXT = "texto de teste com acentuacao: coracao, atencao"

    def run_deliver(self, result, text=None, elevated=False):
        app = self.make_app()
        text = self.TEXT if text is None else text
        saved, calls = [], []

        def fake_deliver(t, cfg=None):
            calls.append(t)
            return result

        with mock.patch.object(_app, "foreground_is_elevated", lambda: elevated), \
                mock.patch.object(_inject, "deliver", fake_deliver), \
                mock.patch.object(_inject, "clip_set_text", saved.append):
            out = app._deliver(text)
        return app, out, saved, calls

    def assertDelivered(self, app, out, saved, mode, chars):
        """Sucesso de entrega: nada de clipboard, de aviso e de icone vermelho."""
        self.assertEqual(out, (mode, chars))
        self.assertEqual(saved, [])
        self.assertNotIn(_app.MSG_ELEVATED_CLIP, app.overlay.messages)
        self.assertNotIn(_app.MSG_INJECT_FAIL, app.overlay.messages)
        self.assertNotIn(_app.MSG_INJECT_PARTIAL, app.overlay.messages)
        self.assertNotIn("error", app.tray.states)
        self.assertEqual(app.tray.notices, [])


# --------------------------------------------------------------------------- #
# 6. `blocked` x `target_elevated` (HIGH)
# --------------------------------------------------------------------------- #

class BlockedVsElevatedTest(_DeliverCase):
    """Rodar o wisper elevado e' a solucao RECOMENDADA para o hook sobreviver a
    janelas de administrador (ARCHITECTURE.md secao 3). Nesse arranjo
    `target_elevated` e' True o tempo todo e a injecao passa normalmente: agir
    na resposta crua fazia toda janela de admin virar "falhou", o texto entrava
    na janela E ia para o clipboard, e o Ctrl+V do usuario escrevia tudo pela
    segunda vez. So `blocked` (alvo elevado E nos nao) autoriza o fallback."""

    def test_an_elevated_target_that_is_not_blocked_is_a_success(self):
        res = _inject.Delivery("type", chars=_units(self.TEXT),
                               target_elevated=True, blocked=False)
        app, out, saved, calls = self.run_deliver(res)
        self.assertDelivered(app, out, saved, "type", _units(self.TEXT))
        self.assertEqual(calls, [self.TEXT])

    def test_a_blocked_target_still_falls_back_to_the_clipboard(self):
        # O outro lado da moeda: sem wisper elevado o UIPI engole SendInput e
        # Ctrl+V com GetLastError() == 0, e o `chars` mente. O clipboard e' a
        # unica saida, e ela nao pode ter sido perdida junto com o alarme falso.
        res = _inject.Delivery("type", chars=_units(self.TEXT),
                               target_elevated=True, blocked=True)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])
        self.assertIn(_app.MSG_ELEVATED_CLIP, app.overlay.messages)
        self.assertIn("error", app.tray.states)

    def test_blocked_is_read_before_chars(self):
        # Sob UIPI o SendInput devolve sucesso: `chars` chega cheio e mentindo.
        # Se o app olhasse `chars` primeiro, anunciaria entrega perfeita.
        res = _inject.Delivery("paste", chars=9999, target_elevated=True, blocked=True)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])

    def test_an_old_inject_without_blocked_still_uses_the_raw_answer(self):
        # Degradar, nunca quebrar: sem a resposta acionavel, "janela de admin"
        # e' o melhor palpite que existe e continua valendo.
        res = _LegacyDelivery("type", chars=_units(self.TEXT), target_elevated=True)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])
        self.assertIn(_app.MSG_ELEVATED_CLIP, app.overlay.messages)

    def test_blocked_none_with_a_plain_target_is_a_success(self):
        # "Nao deu para saber" nao pode disparar nada sozinho.
        res = _inject.Delivery("type", chars=_units(self.TEXT),
                               target_elevated=None, blocked=None)
        app, out, saved, _ = self.run_deliver(res)
        self.assertDelivered(app, out, saved, "type", _units(self.TEXT))

    def test_a_blocked_false_with_an_unknown_target_is_a_success(self):
        res = _inject.Delivery("paste", chars=_units(self.TEXT),
                               target_elevated=None, blocked=False)
        app, out, saved, _ = self.run_deliver(res)
        self.assertDelivered(app, out, saved, "paste", _units(self.TEXT))

    def test_our_own_precheck_still_wins_before_inject_is_called(self):
        # A checagem local continua em primeiro lugar: com a janela em foco
        # elevada nem se chama o inject.
        res = _inject.Delivery("type", chars=_units(self.TEXT), blocked=False)
        app, out, saved, calls = self.run_deliver(res, elevated=True)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(calls, [])
        self.assertEqual(saved, [self.TEXT])


class _Win32Elevation:
    """Simula a camada Win32 do `foreground_is_elevated()`.

    Nenhuma dessas funcoes esta na lista negra do `_safety` (sao todas leitura),
    mas o teste precisa DITAR os dois niveis de integridade, e isso nao da para
    fazer com a janela de verdade do usuario em foco. Os dubles ficam nos objetos
    `_u32`/`_k32` do proprio `app.py` -- cada `ctypes.WinDLL(...)` e' uma
    instancia separada, entao nenhum outro modulo enxerga isto.
    """

    HIGH = 0x3000        # SECURITY_MANDATORY_HIGH_RID: elevado
    MEDIUM = 0x2000      # SECURITY_MANDATORY_MEDIUM_RID: usuario comum

    def __init__(self, theirs, ours):
        self.theirs = theirs
        self.ours = ours
        self.pid = _os.getpid() + 1      # qualquer um que nao seja o nosso

    def _set_pid(self, hwnd, ptr):
        ptr._obj.value = self.pid        # byref() guarda o original em `_obj`
        return 1

    def apply(self, case):
        patches = [
            mock.patch.object(_app._u32, "GetForegroundWindow", lambda: 0x00A1),
            mock.patch.object(_app._u32, "GetWindowThreadProcessId", self._set_pid),
            mock.patch.object(_app._k32, "OpenProcess", lambda a, b, c: 0x1234),
            mock.patch.object(_app._k32, "CloseHandle", lambda h: 1),
            mock.patch.object(_app, "_integrity_level", lambda h: self.theirs),
            mock.patch.object(_app, "_own_integrity_level", lambda: self.ours),
        ]
        for p in patches:
            p.start()
            case.addCleanup(p.stop)
        return self


@unittest.skipUnless(_app, _why_app or "wispr.app indisponivel")
class ForegroundElevationIsRelativeTest(unittest.TestCase):
    """A pre-checagem local tem que ser RELATIVA, e nao "a janela e' de admin?".

    E' o outro lado da mesma moeda do `blocked`: se esta funcao voltar a
    responder a pergunta crua, o ditado nem chega no inject -- toda janela de
    administrador cai no clipboard de novo e o `blocked` do inject nunca e'
    consultado. Consertar um dos dois lados sem o outro nao conserta nada.
    """

    def test_a_target_above_us_is_elevated(self):
        _Win32Elevation(theirs=_Win32Elevation.HIGH,
                        ours=_Win32Elevation.MEDIUM).apply(self)
        self.assertTrue(_app.foreground_is_elevated())

    def test_the_same_level_is_not_elevated(self):
        # Wisper elevado (tarefa agendada, ARCHITECTURE.md secao 3) com uma
        # janela de admin em foco: a injecao PASSA.
        _Win32Elevation(theirs=_Win32Elevation.HIGH,
                        ours=_Win32Elevation.HIGH).apply(self)
        self.assertFalse(_app.foreground_is_elevated())

    def test_a_target_below_us_is_not_elevated(self):
        _Win32Elevation(theirs=_Win32Elevation.MEDIUM,
                        ours=_Win32Elevation.HIGH).apply(self)
        self.assertFalse(_app.foreground_is_elevated())

    def test_an_unreadable_level_is_never_a_block(self):
        # "Nao sei" nao pode mandar o ditado para o clipboard sozinho.
        _Win32Elevation(theirs=None, ours=_Win32Elevation.MEDIUM).apply(self)
        self.assertFalse(_app.foreground_is_elevated())
        _Win32Elevation(theirs=_Win32Elevation.HIGH, ours=None).apply(self)
        self.assertFalse(_app.foreground_is_elevated())


class ElevatedEndToEndTest(_DeliverCase):
    """O cenario do defeito INTEIRO, sem dublar a pre-checagem do app: so a
    camada Win32 e' simulada, e as duas decisoes (a local e a do inject) rodam
    de verdade."""

    def deliver_for_real(self, res, theirs, ours):
        _Win32Elevation(theirs=theirs, ours=ours).apply(self)
        app = self.make_app()
        saved, calls = [], []

        def fake_deliver(t, cfg=None):
            calls.append(t)
            return res

        with mock.patch.object(_inject, "deliver", fake_deliver), \
                mock.patch.object(_inject, "clip_set_text", saved.append):
            out = app._deliver(self.TEXT)
        return app, out, saved, calls

    def test_wisper_elevated_into_an_elevated_window_just_works(self):
        res = _inject.Delivery("type", chars=_units(self.TEXT),
                               target_elevated=True, blocked=False)
        app, out, saved, calls = self.deliver_for_real(
            res, _Win32Elevation.HIGH, _Win32Elevation.HIGH)
        self.assertDelivered(app, out, saved, "type", _units(self.TEXT))
        self.assertEqual(calls, [self.TEXT])      # o inject foi mesmo chamado

    def test_an_ordinary_wisper_into_an_elevated_window_still_falls_back(self):
        res = _inject.Delivery("type", chars=_units(self.TEXT),
                               target_elevated=True, blocked=True)
        app, out, saved, calls = self.deliver_for_real(
            res, _Win32Elevation.HIGH, _Win32Elevation.MEDIUM)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])
        self.assertEqual(calls, [])               # nem chega a tentar injetar
        self.assertIn(_app.MSG_ELEVATED_CLIP, app.overlay.messages)


# --------------------------------------------------------------------------- #
# 7. envio parcial: `chars` cumulativo e proibicao de redigitar/colar por cima
# --------------------------------------------------------------------------- #

class _BlobRecorder:
    """Duble do `_send_blob`: conta eventos por chamada e falha quando mandado.

    Nunca chama SendInput -- e' exatamente o ponto: o teste exercita a
    contabilidade do `type_unicode`, nao o teclado do usuario.
    """

    def __init__(self, ok_calls=1, sent_on_failure=0):
        self.calls = []              # eventos de cada blob recebido
        self.ok_calls = ok_calls
        self.sent_on_failure = sent_on_failure

    def __call__(self, blob):
        n = len(blob) // _inject.SIZEOF_INPUT
        self.calls.append(n)
        if len(self.calls) > self.ok_calls:
            # A falha do UIPI e' exatamente assim: sem codigo de erro nenhum.
            exc = OSError(0, "SendInput sintetico: descartado em silencio")
            exc.sent = self.sent_on_failure
            raise exc
        return n

    @property
    def units(self):
        """Code units de cada blob: cada uma custa keydown + keyup."""
        return [n // 2 for n in self.calls]


@unittest.skipUnless(_inject, _why_inject or "wispr.inject indisponivel")
class PartialSendTest(unittest.TestCase):
    """O bloco 1 entrou e o 2 foi descartado: enquanto `.sent` era o do bloco que
    falhou, isso era indistinguivel de "nada entrou". O app via chars=0, caia no
    clipboard, e o usuario colava os primeiros 64 caracteres uma SEGUNDA vez."""

    TEXT = "a" * 100

    def cfg(self, **over):
        cfg = _config.Config(dict(_config.DEFAULTS))
        cfg.update(over)
        return cfg

    def test_type_unicode_reports_the_units_already_delivered(self):
        rec = _BlobRecorder(ok_calls=1)
        with mock.patch.object(_inject, "_send_blob", rec):
            with self.assertRaises(OSError) as ctx:
                _inject.type_unicode(self.TEXT)
        self.assertEqual(rec.units, [64, 36])
        self.assertEqual(getattr(ctx.exception, "sent", None), 64)

    def test_the_units_of_the_failing_block_count_too(self):
        # 9 eventos aceitos = 4 code units inteiras (keydown + keyup). Divisao
        # inteira: um keydown sem keyup nao mostra glifo nenhum.
        rec = _BlobRecorder(ok_calls=1, sent_on_failure=9)
        with mock.patch.object(_inject, "_send_blob", rec):
            with self.assertRaises(OSError) as ctx:
                _inject.type_unicode(self.TEXT)
        self.assertEqual(getattr(ctx.exception, "sent", None), 64 + 4)

    def test_a_surrogate_pair_is_never_split_across_blocks(self):
        # O emoji cai bem na borda do bloco: mandar metade do par faz o alvo
        # receber dois caracteres invalidos no lugar do glifo.
        text = "a" * 63 + "\U0001F600" + "b" * 10
        rec = _BlobRecorder(ok_calls=1)
        with mock.patch.object(_inject, "_send_blob", rec):
            with self.assertRaises(OSError) as ctx:
                _inject.type_unicode(text)
        self.assertEqual(rec.units[0], 65)         # 63 + o par inteiro
        self.assertEqual(getattr(ctx.exception, "sent", None), 65)

    def test_a_full_type_returns_the_total(self):
        # Controle: sem falha, a contagem e' o total de code units.
        rec = _BlobRecorder(ok_calls=99)
        with mock.patch.object(_inject, "_send_blob", rec):
            got = _inject.type_unicode(self.TEXT)
        self.assertEqual(got, _units(self.TEXT))

    def deliver(self, cfg, rec, paste=None):
        """`inject.deliver()` com tudo que encosta no SO dublado."""
        patches = [
            mock.patch.object(_inject, "_send_blob", rec),
            mock.patch.object(_inject, "_release_stuck_modifiers", lambda: None),
            mock.patch.object(_inject, "target_is_elevated", lambda: False),
            mock.patch.object(_inject, "injection_blocked", lambda: False),
        ]
        if paste is not None:
            patches.append(mock.patch.object(_inject, "paste_text", paste))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return _inject.deliver(self.TEXT, cfg)

    def test_deliver_reports_the_partial_delivery_and_does_not_retype(self):
        rec = _BlobRecorder(ok_calls=1)
        res = self.deliver(self.cfg(inject_mode="type"), rec)
        self.assertEqual(res.chars, 64)
        self.assertEqual(res.mode, "type")
        self.assertFalse(res.empty)
        # DUAS chamadas: o bloco que entrou e o que falhou. Uma terceira seria a
        # redigitacao do texto inteiro, ou seja, os 64 primeiros caracteres
        # aparecendo duas vezes na janela do usuario.
        self.assertEqual(len(rec.calls), 2)

    def test_a_paste_that_never_lands_types_once_and_only_once(self):
        # O Ctrl+V nao chegou, a digitacao entra no lugar e falha no meio: o
        # caminho que mais tentava redigitar do zero.
        rec = _BlobRecorder(ok_calls=1)
        pasted = {"pasted": False, "restored": True, "lost_nontext_formats": False}
        res = self.deliver(self.cfg(inject_mode="paste"), rec,
                           paste=lambda *a, **k: dict(pasted))
        self.assertEqual(res.chars, 64)
        self.assertEqual(res.mode, "type")     # o que esta na janela foi DIGITADO
        self.assertEqual(len(rec.calls), 2)

    def test_nothing_delivered_still_allows_the_type_fallback(self):
        # O outro lado: quando NADA entrou, tentar digitar e' seguro e continua
        # acontecendo -- senao o defeito teria sido "consertado" matando o
        # fallback inteiro.
        rec = _BlobRecorder(ok_calls=99)
        pasted = {"pasted": False, "restored": True, "lost_nontext_formats": False}
        res = self.deliver(self.cfg(inject_mode="paste"), rec,
                           paste=lambda *a, **k: dict(pasted))
        self.assertEqual(res.mode, "type")
        self.assertEqual(res.chars, _units(self.TEXT))

    def test_deliver_never_raises_even_with_a_broken_send(self):
        rec = _BlobRecorder(ok_calls=0)
        res = self.deliver(self.cfg(inject_mode="type"), rec)
        self.assertEqual(res.chars, 0)
        self.assertIsInstance(res, _inject.Delivery)


class PartialInjectionIsNotPastedOverTest(_DeliverCase):
    """O comeco do texto JA esta dentro do e-mail do usuario: um Ctrl+V com o
    texto inteiro colaria o pedaco que ja entrou pela segunda vez."""

    def test_a_partial_delivery_never_reaches_the_clipboard(self):
        res = _inject.Delivery("type", chars=10, blocked=False)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_PARTIAL, 10))
        self.assertEqual(saved, [])                       # nada de colar por cima
        self.assertIn(_app.MSG_INJECT_PARTIAL, app.overlay.messages)
        self.assertNotIn(_app.MSG_INJECT_FAIL, app.overlay.messages)
        self.assertIn("error", app.tray.states)           # falha de entrega, icone vermelho

    def test_zero_chars_is_the_only_case_that_may_use_the_clipboard(self):
        # Controle: com NADA entregue, colar e' seguro e continua acontecendo.
        res = _inject.Delivery("type", chars=0, blocked=False)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])

    def test_an_emoji_is_not_a_false_partial(self):
        # 1 code point em Python, 2 code units no SendInput: comparar com
        # len(text) faria toda entrega com emoji parecer incompleta.
        text = "oi \U0001F600"
        res = _inject.Delivery("type", chars=_units(text), blocked=False)
        app, out, saved, _ = self.run_deliver(res, text=text)
        self.assertDelivered(app, out, saved, "type", _units(text))

    def test_text_that_shrinks_in_sanitize_is_not_a_false_partial(self):
        # O `inject.sanitize()` derruba o U+200B: o `chars` vem menor que o
        # texto cru sem que nada tenha falhado.
        text = "ab​c"
        res = _inject.Delivery("type", chars=3, blocked=False)
        app, out, saved, _ = self.run_deliver(res, text=text)
        self.assertDelivered(app, out, saved, "type", 3)

    def test_the_pill_keeps_the_partial_warning_to_the_end_of_the_job(self):
        # `overlay.hide()` zera o `msg_until`: chamado depois da entrega, ele
        # apagaria no mesmo instante a unica explicacao que o usuario recebe.
        app = self.make_app()
        app._state = "transcribing"
        app._session = 3
        app._deliver = lambda text: (_app._MODE_PARTIAL, 5)
        app._get_engine = lambda: types.SimpleNamespace(
            backend="cpu", transcribe=lambda x: "texto ditado longo")
        app._process(3, np.full(4800, 0.2, np.float32), "ok", datetime.now(), 1.0)
        self.assertNotIn("hide", app.overlay.calls)
        self.assertEqual(app.state, "idle")


# --------------------------------------------------------------------------- #
# 8. transcricao vazia nunca vai para a area de transferencia
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_inject, _why_inject or "wispr.inject indisponivel")
class EmptyTranscriptInjectTest(unittest.TestCase):
    """Um ditado que virou so' um U+200B nao e' falha de entrega: e' texto que
    nao existe. Mandar isso para o clipboard apaga o que o usuario tinha
    guardado la."""

    def deliver(self, text):
        touched = []

        def bomb(*a, **k):
            touched.append("io")
            raise AssertionError("o caminho de texto vazio encostou no SO")

        probes = []
        with mock.patch.object(_inject, "_send_blob", bomb), \
                mock.patch.object(_inject, "paste_text", bomb), \
                mock.patch.object(_inject, "clip_set_text", bomb), \
                mock.patch.object(_inject, "target_is_elevated",
                                  lambda: probes.append("target")), \
                mock.patch.object(_inject, "injection_blocked",
                                  lambda: probes.append("blocked")):
            res = _inject.deliver(text, _config.Config(dict(_config.DEFAULTS)))
        return res, touched, probes

    def test_a_zero_width_space_is_empty_not_a_failure(self):
        res, touched, probes = self.deliver("​")
        self.assertEqual(res.mode, "empty")
        self.assertTrue(res.empty)
        self.assertEqual(res.chars, 0)
        self.assertEqual(touched, [])          # nada de clipboard, nada de SendInput
        self.assertEqual(probes, [])           # nem chegou a perguntar pelo UIPI

    def test_the_empty_return_carries_every_attribute(self):
        # O contrato diz "os seis existem em TODO retorno", inclusive neste.
        res, _, _ = self.deliver("   \r\n  ")
        for attr in ("mode", "chars", "target_elevated", "blocked",
                     "lost_nontext_formats", "empty"):
            self.assertIn(attr, res.as_dict())
        self.assertIsNone(res.blocked)         # None = nao foi consultado
        self.assertIsNone(res.target_elevated)


class EmptyTranscriptAppTest(_DeliverCase):
    """Do lado do app: 'nao havia texto' nao pode virar icone vermelho, aviso de
    falha nem string vazia na area de transferencia."""

    def test_the_empty_delivery_never_touches_the_clipboard(self):
        res = _inject.Delivery("empty", chars=0, blocked=None, empty=True)
        app, out, saved, _ = self.run_deliver(res, text="​")
        self.assertEqual(out, (_app._MODE_EMPTY, 0))
        self.assertEqual(saved, [])
        self.assertNotIn(_app.MSG_INJECT_FAIL, app.overlay.messages)
        self.assertIn(_app.MSG_NOTHING, app.overlay.messages)
        self.assertNotIn("error", app.tray.states)
        self.assertEqual(app.tray.notices, [])

    def test_an_old_inject_without_the_empty_flag_is_caught_by_the_text(self):
        # Cinto e suspensorio: sem o atributo, o texto que morre no sanitize()
        # tambem nao pode ir parar no clipboard.
        res = _LegacyDelivery("type", chars=0)
        app, out, saved, _ = self.run_deliver(res, text="​")
        self.assertEqual(out, (_app._MODE_EMPTY, 0))
        self.assertEqual(saved, [])
        self.assertNotIn(_app.MSG_INJECT_FAIL, app.overlay.messages)

    def test_the_signal_from_inject_is_what_gets_consumed(self):
        # Isola o SINAL do inject. Aqui o texto SOBREVIVE ao `sanitize()`, entao
        # o cinto de seguranca que mede o tamanho esperado nao ajudaria: quem
        # decide e' o `empty` que veio do inject. Sem consumir esse campo, um
        # `chars == 0` desses viraria clipboard mais "Nao consegui inserir o
        # texto" -- a area de transferencia do usuario sobrescrita por causa de
        # um erro que nao aconteceu.
        res = _inject.Delivery("empty", chars=0, blocked=False, empty=True)
        app, out, saved, _ = self.run_deliver(res, text="texto que o inject nao entregou")
        self.assertEqual(out, (_app._MODE_EMPTY, 0))
        self.assertEqual(saved, [])
        self.assertIn(_app.MSG_NOTHING, app.overlay.messages)
        self.assertNotIn(_app.MSG_INJECT_FAIL, app.overlay.messages)
        self.assertNotIn("error", app.tray.states)

    def test_a_real_text_that_failed_still_uses_the_clipboard(self):
        # Controle: a diferenca entre "nao havia texto" e "o texto nao entrou"
        # tem que continuar existindo nos DOIS sentidos.
        res = _inject.Delivery("type", chars=0, blocked=False)
        app, out, saved, _ = self.run_deliver(res)
        self.assertEqual(out, (_app._MODE_CLIPBOARD, 0))
        self.assertEqual(saved, [self.TEXT])
        self.assertIn(_app.MSG_INJECT_FAIL, app.overlay.messages)


# --------------------------------------------------------------------------- #
# 9. `_mic_alive`: na duvida vivo, e nunca lendo `stream.active` daqui
# --------------------------------------------------------------------------- #

class _ActiveProbe:
    """Stream cujo `.active` REGISTRA quem o leu.

    Registrar em vez de levantar de proposito: um `raise` seria engolido pelo
    `except Exception` do `_mic_alive` e o teste passaria com o defeito no lugar.
    """

    def __init__(self):
        self.reads = []

    @property
    def active(self):
        self.reads.append("active")
        return True


@unittest.skipUnless(_app and _audio, _why_app or _why_audio or "modulos indisponiveis")
class MicLivenessTest(_AppCase):
    """`mic_ok` False prende TODO Win+A em "Microfone indisponivel" com o
    microfone funcionando -- o app morto em silencio que o wisper existe para
    evitar. E ler `stream.active` daqui e' `Pa_IsStreamActive` num ponteiro que
    o supervisor pode estar liberando: crash de processo que nenhum
    `except Exception` pega (ARCHITECTURE.md secao 4)."""

    def test_a_mic_without_the_stream_attribute_is_assumed_alive(self):
        app = self.make_app()
        app.mic = types.SimpleNamespace()       # outra versao do modulo, um duble
        self.assertTrue(app.mic_ok)

    def test_the_liveness_read_goes_through_audios_serialised_helper(self):
        app = self.make_app()
        stream = _ActiveProbe()
        app.mic = types.SimpleNamespace(stream=stream)
        seen = []

        def fake_stream_active(s, unknown=False):
            seen.append((s, unknown))
            return True

        with mock.patch.object(_audio, "_stream_active", fake_stream_active):
            ok = app.mic_ok

        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], stream)
        # unknown=True: lock do PortAudio ocupado quer dizer "outra thread esta
        # mexendo no stream", nunca "o microfone morreu".
        self.assertTrue(seen[0][1])
        self.assertEqual(stream.reads, [])     # ninguem leu `.active` de fora do lock

    def test_the_public_report_is_the_fallback_when_the_helper_is_gone(self):
        app = self.make_app()
        stream = _ActiveProbe()
        app.mic = types.SimpleNamespace(stream=stream,
                                        report=lambda: {"active": True})
        with mock.patch.object(_audio, "_stream_active", None):
            self.assertTrue(app.mic_ok)
        self.assertEqual(stream.reads, [])     # `report()` serializa igual

    def test_a_stream_that_is_none_is_really_dead(self):
        # `None` e' o proprio audio.py dizendo que nao ha stream AGORA: e' a
        # unica leitura que pode recusar a gravacao.
        app = self.make_app()
        app.mic = types.SimpleNamespace(stream=None)
        self.assertFalse(app.mic_ok)

    def test_no_mic_at_all_is_dead(self):
        app = self.make_app()
        app.mic = None
        self.assertFalse(app.mic_ok)

    def test_a_dead_stream_reported_by_audio_is_dead(self):
        app = self.make_app()
        app.mic = types.SimpleNamespace(stream=_ActiveProbe())
        with mock.patch.object(_audio, "_stream_active", lambda s, unknown=False: False):
            self.assertFalse(app.mic_ok)

    def test_a_mic_without_the_attribute_does_not_block_a_dictation(self):
        # O sintoma completo: com `mic_ok` preso em False o Win+A nem gravava.
        app = self.make_app()
        app.mic = _FakeMic()
        del app.mic.stream
        app.on_start()
        self.assertEqual(app.state, "recording")
        self.assertNotIn(_app.MSG_MIC_DEAD, app.overlay.messages)


# --------------------------------------------------------------------------- #
# 10. job cancelado velho nao esconde a pilula da sessao nova
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_app, _why_app or "wispr.app indisponivel")
class StaleCancelledJobTest(_AppCase):
    """O Esc deixa a transcricao antiga terminar de proposito, para o usuario
    poder ditar de novo na hora. Quando ela termina, a pilula na tela ja e' a da
    gravacao DE AGORA: esconde-la deixa o `set_level` alimentando uma barrinha
    invisivel e o usuario falando sem nenhum retorno ate' o proximo Enter."""

    def audio(self):
        return np.full(4800, 0.2, np.float32)

    def app_with_stale_job(self, session):
        app = self.make_app()
        app._state = "recording"        # a sessao NOVA, ja gravando
        app._session = session
        app._cancelled = 5
        app.hotkey.recording = True
        app.hotkey.cancellable = True
        app._get_engine = lambda: types.SimpleNamespace(
            backend="cpu", transcribe=lambda x: "texto da sessao cancelada")
        app._deliver = lambda text: self.fail("o job cancelado injetou texto")
        return app

    def test_the_new_sessions_pill_survives_the_old_job(self):
        app = self.app_with_stale_job(6)
        app._process(5, self.audio(), "ok", datetime.now(), 1.0)
        self.assertNotIn("hide", app.overlay.calls)
        self.assertEqual(app.state, "recording")

    def test_the_new_sessions_hotkey_flags_survive_too(self):
        # Mesma raiz: limpar as flags aqui roubaria o Enter e o Esc do ditado
        # que esta acontecendo agora.
        app = self.app_with_stale_job(6)
        app._process(5, self.audio(), "ok", datetime.now(), 1.0)
        self.assertTrue(app.hotkey.recording)
        self.assertTrue(app.hotkey.cancellable)

    def test_without_a_new_session_the_pill_is_hidden(self):
        # Controle: quando a pilula ainda e' do job cancelado, ela SOME.
        app = self.app_with_stale_job(5)
        app._state = "transcribing"
        app.hotkey.recording = False
        app._process(5, self.audio(), "ok", datetime.now(), 1.0)
        self.assertIn("hide", app.overlay.calls)
        self.assertEqual(app.state, "idle")
        self.assertFalse(app.hotkey.cancellable)


# --------------------------------------------------------------------------- #
# 11. a bandeja tem que DESCER do vermelho quando o hook volta
# --------------------------------------------------------------------------- #

class _BrokenStateApp:
    """App ilegivel: a property `state` levanta (boot pela metade, shutdown)."""

    def __init__(self, cfg, hotkey, mic_ok=True):
        self.cfg = cfg
        self.hotkey = hotkey
        self.mic_ok = mic_ok

    @property
    def state(self):
        raise RuntimeError("App no meio do boot")


@unittest.skipUnless(_tray and _config, _why_tray or "wispr.tray indisponivel")
class TrayErrorLatchTest(unittest.TestCase):
    """`App.on_hook_error()` NAO arma timer de volta ao verde: ele conta com a
    bandeja reavaliar `hotkey.alive` a cada repintura. Enquanto isto so subia, o
    watchdog reinstalava o hook segundos depois, o Win+A voltava a funcionar e o
    icone ficava vermelho ate' o app ser reiniciado."""

    def tray(self, app_state="idle", alive=True, mic_ok=True, broken=False):
        cfg = _config.Config(dict(_config.DEFAULTS))
        hk = types.SimpleNamespace(alive=alive, installs=2, failed_installs=1)
        if broken:
            app = _BrokenStateApp(cfg, hk, mic_ok)
        else:
            app = types.SimpleNamespace(cfg=cfg, hotkey=hk, state=app_state,
                                        mic_ok=mic_ok)
        return _tray.Tray(app)     # so o objeto; `run()` (pystray) nunca e' chamado

    def test_the_red_goes_away_once_the_hook_is_back(self):
        self.assertEqual(self.tray(app_state="idle")._effective_state("error"), "idle")

    def test_it_comes_back_to_the_real_state_of_the_app(self):
        for app_state, shown in (("recording", "recording"),
                                 ("transcribing", "working"),
                                 ("injecting", "working"),
                                 ("idle", "idle")):
            with self.subTest(app_state=app_state):
                t = self.tray(app_state=app_state)
                self.assertEqual(t._effective_state("error"), shown)

    def test_a_dead_hook_keeps_the_red(self):
        self.assertEqual(self.tray(alive=False)._effective_state("error"), "error")

    def test_an_unknown_hook_keeps_the_red(self):
        # Descer exige prova positiva: "nao sei" nunca absolve.
        self.assertEqual(self.tray(alive=None)._effective_state("error"), "error")

    def test_a_dead_mic_keeps_the_red(self):
        # O outro vermelho sem timer: captura ausente no boot.
        self.assertEqual(self.tray(mic_ok=False)._effective_state("error"), "error")

    def test_an_unreadable_app_keeps_the_red(self):
        self.assertEqual(self.tray(broken=True)._effective_state("error"), "error")

    def test_an_app_in_error_keeps_the_red(self):
        self.assertEqual(self.tray(app_state="error")._effective_state("error"), "error")

    def test_the_climb_is_untouched(self):
        # Sem hook nao existe Win+A: o repouso continua virando vermelho.
        self.assertEqual(self.tray(alive=False)._effective_state("idle"), "error")
        self.assertEqual(self.tray(alive=True)._effective_state("idle"), "idle")
        self.assertEqual(self.tray(alive=None)._effective_state("idle"), "idle")

    def test_a_live_state_is_never_overridden(self):
        # 'gravando' e 'trabalhando' sao estados vivos e informativos.
        self.assertEqual(self.tray(alive=False)._effective_state("recording"), "recording")
        self.assertEqual(self.tray(alive=False)._effective_state("working"), "working")

    def test_the_painted_state_follows_the_hook_without_a_new_set_state(self):
        # O caminho de verdade: `set_state('error')` guardou 'error', e a
        # repintura seguinte (o cabecalho do menu) ja mostra o estado real.
        t = self.tray(app_state="idle")
        t.set_state("error")
        self.assertEqual(t._state, "error")          # o guardado continua vermelho
        self.assertEqual(t._effective_state(t._state), "idle")
        header = t._header_text()
        self.assertIn("pronto", header)
        self.assertNotIn("erro", header)


# --------------------------------------------------------------------------- #
# 12. repaste terminado nao pode deixar o Esc do usuario preso
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_app, _why_app or "wispr.app indisponivel")
class RepasteClearsTheFlagsTest(_AppCase):
    """Flag presa = tecla sumida no Windows INTEIRO. O repaste estaciona a
    maquina em 'injecting'; um Win+A que chegue nesse meio tempo e' recusado sem
    limpar nada (ali normalmente existe um ditado vivo), e o engine ja ligou
    `cancellable` ao entregar o 'start'. Sem limpeza no fim do repaste o hook
    passava a engolir o Esc do sistema ate' comer um Esc do usuario."""

    def join_repaste(self, timeout=10.0):
        for t in threading.enumerate():
            if t.name == "repaste":
                t.join(timeout)
                self.assertFalse(t.is_alive(), "a thread do repaste nao terminou")

    def test_a_refused_win_a_during_a_repaste_leaves_no_swallowed_key(self):
        app = self.make_app()
        app.last_text = "texto para reinserir"
        started, release = threading.Event(), threading.Event()

        def slow_deliver(text):
            started.set()
            release.wait(10.0)
            return "type", len(text)

        app._deliver = slow_deliver
        app.repaste_last()
        try:
            self.assertTrue(started.wait(10.0), "o repaste nem comecou")
            self.assertEqual(app.state, "injecting")
            app.on_start()                       # Win+A no meio do repaste
            self.assertIn(_app.MSG_BUSY, app.overlay.messages)
            self.assertEqual(app.state, "injecting")
            # O engine liga `cancellable` ao ENTREGAR o start, mesmo com o app
            # recusando: dai em diante o Esc pertence a um ditado que nao existe.
            app.hotkey.cancellable = True
        finally:
            release.set()
            self.join_repaste()

        self.assertFalse(app.hotkey.cancellable)
        self.assertFalse(app.hotkey.recording)
        self.assertEqual(app.state, "idle")

    def test_a_plain_repaste_also_ends_with_the_flags_down(self):
        app = self.make_app()
        app.last_text = "texto para reinserir"
        app.hotkey.cancellable = True
        app._deliver = lambda text: ("type", len(text))
        app.repaste_last()
        self.join_repaste()
        self.assertFalse(app.hotkey.cancellable)
        self.assertEqual(app.state, "idle")

    def test_a_repaste_that_fails_still_clears_the_flags(self):
        app = self.make_app()
        app.last_text = "texto para reinserir"
        app.hotkey.cancellable = True

        def boom(text):
            raise RuntimeError("falha sintetica na reinsercao")

        app._deliver = boom
        app.repaste_last()
        self.join_repaste()
        self.assertFalse(app.hotkey.cancellable)
        self.assertEqual(app.state, "idle")

    def test_a_repaste_whose_thread_never_starts_clears_them_too(self):
        app = self.make_app()
        app.last_text = "texto para reinserir"
        app.hotkey.cancellable = True

        def no_thread(*a, **k):
            raise RuntimeError("sem thread disponivel")

        # So o nome `threading` DENTRO do app.py: o modulo de verdade fica
        # intacto para o resto da suite.
        fake = types.SimpleNamespace(Thread=no_thread)
        with mock.patch.object(_app, "threading", fake):
            app.repaste_last()
        self.assertFalse(app.hotkey.cancellable)
        self.assertEqual(app.state, "idle")

    def test_a_repaste_refused_because_of_a_real_dictation_keeps_the_flags(self):
        # O contrario tambem tem que valer: com um ditado VIVO o repaste e'
        # recusado la em cima e nao pode limpar nada -- essas flags sao dele.
        app = self.make_app()
        app.last_text = "texto para reinserir"
        app._state = "transcribing"
        app.hotkey.cancellable = True
        app._deliver = lambda text: self.fail("o repaste atropelou o ditado")
        app.repaste_last()
        self.join_repaste()
        self.assertTrue(app.hotkey.cancellable)
        self.assertEqual(app.state, "transcribing")
        self.assertIn(_app.MSG_BUSY, app.overlay.messages)

if __name__ == "__main__":
    unittest.main(verbosity=2)
