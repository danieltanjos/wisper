# -*- coding: utf-8 -*-
"""Captura de microfone do wisper: WASAPI compartilhado, stream sempre aberto.

O stream fica aberto o tempo todo e escreve num ring buffer. O Win+A só marca uma
posicao (`mark()`), rebobinada pelo pre-roll, e o `take()` recorta o trecho. Isso
existe pelo **pre-roll**, nao pela latencia: sem ele a primeira silaba some.

Exige interpretador do python.org. O build da Microsoft Store tem identidade de
pacote com entrada propria no ConsentStore do microfone: em "Prompt" o
`InputStream()` trava para sempre sem excecao, em "Deny" falha -9999/-9996.
Ver docs/ARCHITECTURE.md secao 4 — tudo aqui foi medido nesta maquina.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    # MTA: as notificacoes de endpoint COM chegam sem precisar de bomba de mensagens.
    # No app isto e' MORTO: `wispr/__init__.py` roda primeiro e ja fixou a flag antes
    # de qualquer submodulo. Fica so para `python wispr/audio.py` direto, onde o
    # `import comtypes` logo abaixo acontece antes do pacote ser inicializado.
    sys.coinit_flags = 0

import contextlib
import ctypes
import logging
import math
import threading
import time
import weakref
from collections import deque
from ctypes import POINTER, c_int, c_uint, c_wchar_p

# ORDEM IMPORTA: importar sounddevice roda Pa_Initialize, cujo backend WASAPI chama
# CoInitializeEx(COINIT_APARTMENTTHREADED) nesta thread. comtypes tem que vir ANTES ou
# o MTA pedido acima perde e o import do comtypes levanta RPC_E_CHANGED_MODE
# (-2147417850). O try/except e' so para o modulo continuar importavel numa maquina
# sem comtypes: quem cobra a dependencia e' o construtor de Mic().
try:
    import comtypes
    from comtypes import (CLSCTX_ALL, COMMETHOD, GUID, HRESULT, CoCreateInstance,
                          COMObject, IUnknown)
    _COM_ERR = None
except Exception as _exc:                          # pragma: no cover - so fora do Windows
    comtypes = None
    _COM_ERR = _exc

import numpy as np
import sounddevice as sd
import soxr
from scipy.signal import butter, sosfilt

import wispr                                        # so para ler wispr.COM_READY vivo
from wispr import config

log = logging.getLogger(__name__)

__all__ = ["CAPTURE_SR", "TARGET_SR", "SILENCE_RMS", "Mic", "to_whisper", "rms",
           "device_report", "resolve_device", "wasapi_input_index",
           "default_comm_mic_name"]

# --------------------------------------------------------------------- defaults
# Servem de fallback quando a chave nao esta no cfg; o cfg sempre ganha.
CAPTURE_SR, TARGET_SR = 48000, 16000               # WASAPI compartilhado so aceita 48k aqui
# RING_SEC e' o teto real de um ditado, nao um detalhe de buffer: o que nao cabe no
# ring e' sobrescrito antes do Enter e sai como meia frase, sem erro nenhum. Casado
# com config.DEFAULTS["ring_sec"]; a 48 kHz float32 sao ~34,5 MB residentes.
RING_SEC, PREROLL_SEC, BLOCK = 180.0, 0.35, 480    # BLOCK = 10 ms
SILENCE_RMS = 3e-5                                 # gate de silencio digital (obrigatorio)
HIGHPASS_HZ = 80
NORMALIZE_DBFS = -3.0

POLL_SEC = 0.5                                     # supervisor; recuperacao medida em 0,75 s
BACKOFF_BASE = 0.5
BACKOFF_MAX = 5.0                                  # device sumido de vez nao pode virar spin
MAX_XRUNS = 64                                     # deque limitada: o app fica semanas de pe
SILENT_EVENT_MIN_S = 1.0                           # so o aviso e' limitado, nunca o gate

# Tetos de sanidade para valores vindos do config.json. Sem eles um `ring_sec`
# datilografado errado (100000) vira uma alocacao de 19 GB e o microfone nunca abre.
# 300 s = ~57,6 MB a 48 kHz float32: cabe folgado e da espaco para quem quiser ditar
# um texto longo de uma vez so sem esbarrar no teto.
MAX_RING_SEC = 300.0
MAX_CAPTURE_SR = 192000
#: Quem NAO e' o supervisor nunca espera indefinidamente pelo PortAudio: sob o
#: Python da Store o `InputStream()` trava para sempre sem excecao, e um menu de
#: bandeja travado junto seria um app congelado sem console para explicar.
PA_LOCK_TIMEOUT = 2.0

_VT_LPWSTR = 31

_EVENT_LEVEL = {
    "open": logging.INFO,
    "device_changed": logging.INFO,
    "stream_dead": logging.WARNING,
    "reopen_failed": logging.ERROR,
    "silent_input": logging.WARNING,
    "lapped": logging.WARNING,
}


# --------------------------------------------------------------------- utilidades
#: Chaves ja avisadas por _clamp(). _num() roda a cada to_whisper(), ou seja uma vez
#: por ditado: sem isso o aviso encheria o rotativo de 1 MB sozinho.
_CLAMPED = set()


def _clamp(key, value, bound):
    """Prende `value` no limite e avisa uma unica vez por chave.

    Devolver o DEFAULT aqui era pior do que o erro de quem editou o config.json:
    `ring_sec: 300` virava 180 em silencio e a pessoa continuava perdendo o comeco
    das falas longas sem nunca entender por que.
    """
    if key not in _CLAMPED:
        _CLAMPED.add(key)
        log.warning("config %r=%r is out of range; clamped to %r", key, value, bound)
    return bound


def _num(cfg, key, default, cast=float, minimum=None, maximum=None):
    """Le um numero do cfg sem nunca deixar um valor torto derrubar a captura."""
    try:
        value = cast(cfg[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        # OverflowError e' o inteiro gigante digitado no config.json: float(10**400)
        # estoura aqui, antes de qualquer verificacao de limite, e sem este ramo a
        # excecao subiria ate matar a abertura do microfone.
        return default
    # isinstance antes do isfinite: math.isfinite(10**400) estoura com OverflowError.
    # NaN/Inf nao tem limite para onde prender, entao so eles voltam ao default.
    if not isinstance(value, int) and not math.isfinite(value):
        return default
    # cast no limite tambem: `block` e `capture_sr` precisam voltar int, e um teto
    # float faria o np.zeros e o blocksize do PortAudio receberem 300.0 em vez de 300.
    if minimum is not None and value < minimum:
        return _clamp(key, value, cast(minimum))
    if maximum is not None and value > maximum:
        return _clamp(key, value, cast(maximum))
    return value


def rms(x) -> float:
    """RMS do bloco, em float64. Usado pelas barras de nivel do overlay.

    Acumula em float64 porque float32 satura o somatorio em blocos longos, e devolve
    0.0 para NaN/Inf: driver quebrado nao pode deixar a barrinha maluca.
    """
    try:
        a = np.asarray(x, dtype=np.float64).reshape(-1)
        if a.size == 0:
            return 0.0
        value = float(np.sqrt(np.dot(a, a) / a.size))
    except Exception:
        return 0.0
    return value if math.isfinite(value) else 0.0


# O estado global do PortAudio (Pa_Initialize/Pa_Terminate, a lista de devices e o
# ponteiro de cada stream) e' tocado por tres threads: o supervisor que reabre, a
# bandeja que pede diagnostico e a principal no shutdown. Pa_Terminate no meio de um
# Pa_GetDeviceInfo, ou um Pa_IsStreamActive num ponteiro que outra thread acabou de
# liberar, e' crash de processo — invisivel sob pythonw. RLock porque _open() chama
# resolve_device() ja com o lock na mao.
_pa_lock = threading.RLock()


@contextlib.contextmanager
def _portaudio(timeout: float = -1.0):
    """Serializa o acesso ao PortAudio. Cede `True` se conseguiu o lock.

    Com `timeout >= 0` desiste em vez de travar: se o `_open()` do supervisor
    pendurar, o shutdown e o menu da bandeja seguem em frente.
    """
    got = False
    try:
        got = _pa_lock.acquire(timeout=timeout)
    except Exception:                              # pragma: no cover - lock nao falha
        got = False
    try:
        yield got
    finally:
        if got:
            _pa_lock.release()


def _stream_active(stream, unknown: bool = False) -> bool:
    """`stream.active` serializado contra quem estiver fechando o stream."""
    if stream is None:
        return False
    with _portaudio(PA_LOCK_TIMEOUT) as got:
        if not got:
            return unknown                         # ocupado != morto
        try:
            return bool(stream.active)
        except Exception:
            return False


def _co_init() -> None:
    """Poe a thread atual no mesmo apartamento COM do processo (MTA).

    `comtypes.CoInitialize()` seria COINIT_APARTMENTTHREADED e jogaria esta thread
    em STA, contra o `sys.coinit_flags = 0` do topo do modulo — e uma STA sem bomba
    de mensagens nao entrega callback de COM nenhum. `CoInitializeEx(flags)` explicito.
    """
    if _COM_ERR is not None:
        return
    try:
        comtypes.CoInitializeEx(getattr(sys, "coinit_flags", 0))
    except Exception:
        pass                                       # RPC_E_CHANGED_MODE: ja ha apartamento, ok


def _require_com() -> None:
    if _COM_ERR is not None:
        raise RuntimeError(
            "wispr.audio precisa do comtypes para resolver o endpoint de captura por COM "
            "(pip install comtypes). O import falhou com: %r" % (_COM_ERR,))


_com_degraded_logged = False


def _com_ready() -> bool:
    """True quando da' para confiar na resolucao de endpoint por COM.

    O `import comtypes` la' de cima NAO prova nada: se o PortAudio chegou primeiro e
    jogou o processo em STA, o comtypes ja esta em `sys.modules` e o import volta
    dele, sorridente, com o apartamento errado. Quem sabe se a ordem foi respeitada
    e' o `wispr/__init__.py`, e ele REBINDA o valor — dai o getattr no modulo, nunca
    um `from wispr import COM_READY` congelado no import.
    """
    if _COM_ERR is not None:
        return False
    try:
        return bool(getattr(wispr, "COM_READY", False))
    except Exception:                              # pragma: no cover - getattr nao falha
        return False


def _com_degraded(where: str) -> None:
    """Registra uma vez que o caminho COM saiu de cena e o WASAPI assumiu."""
    global _com_degraded_logged
    if _com_degraded_logged:
        return
    _com_degraded_logged = True
    log.warning("COM endpoint resolution unavailable at %s (ready=%r, error=%s); "
                "falling back to the PortAudio WASAPI match", where,
                getattr(wispr, "COM_READY", None), getattr(wispr, "COM_ERROR", None))


# --------------------------------------------------------------------- COM: endpoints
_PropVariantClear = None

if _COM_ERR is None:
    # O bloco inteiro dentro de um try: se o comtypes desta maquina (ou o stub da
    # suite de testes) nao der um GUID que sirva de campo de ctypes.Structure, o
    # modulo ainda tem que importar — senao o DSP, que nao usa COM nenhum, some junto.
    try:
        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

        class PROPVARIANT(ctypes.Structure):
            # 24 bytes no x64: vt + 3 reservados (8) + ponteiro (8) + cauda da union (8).
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                        ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                        ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_ulonglong)]

        class IPropertyStore(IUnknown):
            _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(ctypes.c_ulong), "c")),
                COMMETHOD([], HRESULT, "GetAt", (["in"], ctypes.c_ulong, "i"),
                          (["out"], POINTER(PROPERTYKEY), "k")),
                COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "k"),
                          (["out"], POINTER(PROPVARIANT), "v")),
                COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "k"),
                          (["in"], POINTER(PROPVARIANT), "v")),
                COMMETHOD([], HRESULT, "Commit"))

        class IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = (
                COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                          (["in"], c_uint, "ctx"), (["in"], ctypes.c_void_p, "p"),
                          (["out"], POINTER(POINTER(IUnknown)), "i")),
                COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_uint, "a"),
                          (["out"], POINTER(POINTER(IPropertyStore)), "ps")),
                COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "id")),
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint), "st")))

        class IMMNotificationClient(IUnknown):
            _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
            _methods_ = (
                COMMETHOD([], HRESULT, "OnDeviceStateChanged", (["in"], c_wchar_p, "id"),
                          (["in"], ctypes.c_ulong, "s")),
                COMMETHOD([], HRESULT, "OnDeviceAdded", (["in"], c_wchar_p, "id")),
                COMMETHOD([], HRESULT, "OnDeviceRemoved", (["in"], c_wchar_p, "id")),
                COMMETHOD([], HRESULT, "OnDefaultDeviceChanged", (["in"], c_int, "flow"),
                          (["in"], c_int, "role"), (["in"], c_wchar_p, "id")),
                # A PROPERTYKEY vai por VALOR mesmo, como na IDL.
                COMMETHOD([], HRESULT, "OnPropertyValueChanged", (["in"], c_wchar_p, "id"),
                          (["in"], PROPERTYKEY, "k")))

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = (
                COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "f"),
                          (["in"], c_uint, "m"), (["out"], POINTER(POINTER(IUnknown)), "c")),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "flow"),
                          (["in"], c_int, "role"), (["out"], POINTER(POINTER(IMMDevice)), "d")),
                COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "id"),
                          (["out"], POINTER(POINTER(IMMDevice)), "d")),
                COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                          (["in"], POINTER(IMMNotificationClient), "c")),
                COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                          (["in"], POINTER(IMMNotificationClient), "c")))

        class _Watcher(COMObject):
            """IMMNotificationClient: troca de headset chega em 5,2 ms, 0,0000% de CPU
            ociosa — qualquer polling perde disso.

            Em MTA o callback chega numa thread de RPC arbitraria, sem sincronizacao
            com nada nosso: por isso ele SO pode setar um threading.Event, nunca
            tocar no stream, no ring ou no log. Quem reage e' o supervisor.
            Enquanto registrado, o objeto e' mantido vivo por `_watchers` (ref forte
            global), nao pelo Mic — ver o comentario la'."""

            _com_interfaces_ = [IMMNotificationClient]

            def __init__(self, mic_ref):
                super().__init__()
                # weakref de proposito: enquanto o enumerador segurar uma referencia
                # COM, o comtypes mantem este objeto vivo em COMObject._instances_
                # (dict global). Uma ref forte ao Mic aqui deixaria o Mic — e o stream
                # aberto dele — imortais se alguem esquecesse o close().
                self._mic_ref = mic_ref

            def IMMNotificationClient_OnDefaultDeviceChanged(self, this, flow, role, dev_id):
                if flow == eCapture and role == eCommunications:
                    try:
                        mic = self._mic_ref()
                        if mic is not None:
                            mic._retarget.set()
                    except Exception:
                        pass                       # excecao aqui viraria HRESULT de erro
                return 0

            def IMMNotificationClient_OnDeviceStateChanged(self, this, i, s):
                return 0

            def IMMNotificationClient_OnDeviceAdded(self, this, i):
                return 0

            def IMMNotificationClient_OnDeviceRemoved(self, this, i):
                return 0

            def IMMNotificationClient_OnPropertyValueChanged(self, this, i, k):
                return 0

        CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        PKEY_Device_FriendlyName = (GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)

        # argtypes explicito para o ctypes recusar qualquer coisa que nao seja um
        # ponteiro para a nossa PROPVARIANT de 24 bytes. oledll ja poe restype HRESULT
        # com errcheck, entao um HRESULT de falha chega como OSError.
        _PropVariantClear = ctypes.oledll.ole32.PropVariantClear
        _PropVariantClear.argtypes = [POINTER(PROPVARIANT)]
    except Exception as _exc:                      # pragma: no cover - comtypes torto
        _COM_ERR = _exc
        _PropVariantClear = None

eCapture, eCommunications = 1, 2
_enum = None
_enum_lock = threading.Lock()

#: Referencia FORTE e global para cada _Watcher registrado. Em MTA as chamadas do
#: IMMNotificationClient chegam em threads de RPC arbitrarias, a qualquer momento
#: entre o Register e o Unregister. O Mic e' quem guardaria o objeto, mas o Mic e'
#: descartavel: se alguem largar um Mic sem `close()`, o coletor levaria o COMObject
#: junto e a proxima troca de headset chamaria um vtable liberado — isso e' crash de
#: processo, nao excecao, e sob pythonw.exe some sem uma linha de log. Sai daqui
#: somente quando o Unregister confirma. O _Watcher so guarda weakref do Mic, entao
#: isto nao torna Mic nenhum imortal.
_watchers = set()
_watchers_lock = threading.Lock()


def enumerator():
    """IMMDeviceEnumerator em cache. Levanta RuntimeError claro se faltar comtypes."""
    global _enum
    _require_com()
    with _enum_lock:
        if _enum is None:
            _co_init()
            _enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        return _enum


def _drop_enumerator() -> None:
    """Descarta o enumerador em cache.

    Se o servico de audio do Windows reiniciar, o ponteiro cacheado morre e passa a
    devolver RPC_E_DISCONNECTED para sempre — sem isso o app nunca mais resolveria
    um device. Quem registrou notificacao guarda a propria referencia (Mic._watcher_enum)
    para desregistrar no MESMO objeto, como a MSDN exige.
    """
    global _enum
    with _enum_lock:
        _enum = None


def default_comm_mic_name():
    """Friendly name do endpoint de captura padrao de COMUNICACOES.

    Tem que vir do COM: o PortAudio so expoe eConsole e ignora o papel
    eCommunications. Devolve None se qualquer etapa falhar — quem chama tem fallback.
    """
    if not _com_ready():
        # Sem a ordem de import garantida, um GetDefaultAudioEndpoint pode devolver
        # lixo ou travar; o casamento por nome do WASAPI perde o papel
        # eCommunications, mas continua sendo WASAPI e nao o clone MME de 90 ms.
        _com_degraded("default_comm_mic_name")
        return None
    prop = None
    try:
        dev = enumerator().GetDefaultAudioEndpoint(eCapture, eCommunications)
        pkey = PROPERTYKEY()
        pkey.fmtid, pkey.pid = PKEY_Device_FriendlyName
        prop = dev.OpenPropertyStore(0).GetValue(ctypes.byref(pkey))
        if prop.vt == _VT_LPWSTR and prop.pwszVal:   # VT_LPWSTR
            return ctypes.cast(prop.pwszVal, c_wchar_p).value
    except Exception as exc:
        log.debug("default_comm_mic_name failed: %r", exc)
        _drop_enumerator()
    finally:
        # A string do GetValue vem de CoTaskMemAlloc; sem isso vaza um pedacinho por
        # abertura, e o supervisor reabre ate 720 vezes por hora com o mic sumido.
        if prop is not None and _PropVariantClear is not None:
            try:
                _PropVariantClear(ctypes.byref(prop))
            except Exception:
                pass
    return None


def _wasapi_hostapi() -> int:
    for i, host in enumerate(sd.query_hostapis()):
        if host["name"] == "Windows WASAPI":
            return i
    raise RuntimeError("host API 'Windows WASAPI' nao existe neste PortAudio")


def resolve_device(timeout: float = -1.0):
    """Resolve o endpoint de captura. Devolve (indice_sounddevice, friendly_name, fallback).

    NUNCA usar sd.default.device: ele aponta para o clone MME de 90 ms. O casamento
    e' por nome porque o PortAudio trunca o friendly name do WASAPI, dai o
    `name.startswith(d['name'])` alem da igualdade.
    """
    with _portaudio(timeout) as got:
        if not got:
            raise RuntimeError("PortAudio ocupado (outra thread reabrindo o stream)")
        host = _wasapi_hostapi()
        name = default_comm_mic_name()
        if name:
            for i, dev in enumerate(sd.query_devices()):
                # `dev["name"]` vazio casaria com startswith("") em QUALQUER endpoint.
                if (dev["hostapi"] == host and dev["max_input_channels"] > 0 and dev["name"]
                        and (dev["name"] == name or name.startswith(dev["name"]))):
                    return i, name, False
        # Fallback: o default do proprio host WASAPI. Perde o papel eCommunications, mas
        # ainda e' WASAPI — melhor do que cair no MME.
        idx = sd.query_hostapis(host)["default_input_device"]
        if idx is None or int(idx) < 0:
            raise RuntimeError("nenhum dispositivo de entrada WASAPI disponivel")
        idx = int(idx)
        return idx, (name or sd.query_devices(idx)["name"]), True


def wasapi_input_index() -> int:
    """So o indice sounddevice do endpoint resolvido."""
    return resolve_device()[0]


# --------------------------------------------------------------------- captura
_active_mic = None                                 # weakref, so para device_report()


def _live_mic():
    try:
        return _active_mic() if _active_mic is not None else None
    except Exception:                              # pragma: no cover
        return None


def _gate_threshold() -> float:
    """Limiar do gate de silencio: o do Mic vivo, senao a constante do modulo.

    app.py chama `Mic.has_signal(x)` com um argumento so; sem isso ele usaria 3e-5
    enquanto o `take()` do mesmo Mic ja tinha aplicado outro limiar vindo do cfg.
    """
    mic = _live_mic()
    try:
        thr = float(getattr(mic, "silence_rms", SILENCE_RMS))
    except (TypeError, ValueError):
        return SILENCE_RMS
    return thr if math.isfinite(thr) and thr > 0.0 else SILENCE_RMS


class Mic:
    """Captura WASAPI compartilhada, sempre aberta, com ring buffer e supervisor.

    `on_event(name, **kw)` e' chamado para: open, device_changed, stream_dead,
    reopen_failed, silent_input, lapped. O nome do evento vai sempre POSICIONAL e
    o payload nunca traz a chave `name` (o friendly name do endpoint chega como
    `device_name`), senao a chamada colidiria com o proprio parametro do handler.
    Excecao dentro dele e' engolida e logada: o app roda sob pythonw.exe, sem console.
    """

    def __init__(self, cfg=None, on_event=lambda *a, **k: None):
        _require_com()                             # unico ponto que cobra comtypes
        self.cfg = cfg or config.load()
        self.on_event = on_event

        self.sr = int(_num(self.cfg, "capture_sr", CAPTURE_SR, int,
                           minimum=8000, maximum=MAX_CAPTURE_SR))
        self.ring_sec = _num(self.cfg, "ring_sec", RING_SEC, float,
                             minimum=0.5, maximum=MAX_RING_SEC)
        self.preroll_sec = _num(self.cfg, "preroll_sec", PREROLL_SEC, float,
                                minimum=0.0, maximum=MAX_RING_SEC)
        # Pre-roll maior que meio ring faria o mark() apontar para audio que o
        # escritor ja sobrescreveu: o take() devolveria gravacao velha como se fosse
        # a de agora, sem erro nenhum.
        self.preroll_sec = min(self.preroll_sec, self.ring_sec * 0.5)
        self.block = int(_num(self.cfg, "block", BLOCK, int, minimum=32, maximum=self.sr))
        # Gate de silencio digital: nunca pode virar 0 nem sumir. Silencio digital tem
        # RMS exatamente 0.0, entao qualquer limiar positivo o pega.
        self.silence_rms = max(_num(self.cfg, "silence_rms", SILENCE_RMS, float, minimum=0.0),
                               1e-12)
        self.poll_sec = _num(self.cfg, "supervise_sec", POLL_SEC, float,
                             minimum=0.05, maximum=10.0)
        self.backoff_max = _num(self.cfg, "reopen_backoff_max", BACKOFF_MAX, float,
                                minimum=0.5, maximum=300.0)

        self.ring = np.zeros(int(self.sr * self.ring_sec), np.float32)
        # Bloco grande demais para o ring anularia a faixa de guarda do take() e cairia
        # no ramo "frames >= size" do callback a cada chamada.
        self.block = min(self.block, max(32, self.ring.size // 8))
        self.written = 0
        self.reopens = 0                           # reaberturas que DERAM certo
        self.reopen_attempts = 0
        self.device = -1
        self.name = ""
        self.hostapi = ""
        self.fallback = False
        self.stream = None
        self.xruns = deque(maxlen=MAX_XRUNS)
        self.last_audio = np.zeros(0, np.float32)  # guardado para diagnostico/keep_recordings
        self.last_rms = 0.0
        self.last_error = ""

        self._cb_err = ""
        self._dead = False
        self._silent_t = 0.0
        self._stop = threading.Event()
        self._retarget = threading.Event()
        self._watcher = None
        self._watcher_enum = None

        opened = False
        try:
            # Espera COM PRAZO: Mic() nasce na thread do hotkey, logo depois do Win+A, e
            # nesse instante o hook ja esta engolindo Enter e Esc. Se o `mic-sup`
            # estiver pendurado dentro do _reopen de um endpoint que sumiu, esperar
            # sem prazo aqui congelaria o Win+A do usuario sem nada na tela.
            with _portaudio(PA_LOCK_TIMEOUT) as got:
                if not got:
                    raise RuntimeError(
                        "PortAudio ocupado por outra thread (reabertura do endpoint "
                        "em andamento); o supervisor abre a captura no proximo ciclo")
                self._open()
            opened = True
        except Exception as exc:
            # Sem microfone no boot o app ainda sobe; o supervisor fica tentando.
            self.last_error = str(exc)[:160]
            self._dead = True
            self._emit("stream_dead", active=False, err=self.last_error)

        global _active_mic
        _active_mic = weakref.ref(self)
        if opened:
            self._emit_open()

        self._register_watcher()

        try:
            threading.Thread(target=self._supervise, name="mic-sup", daemon=True).start()
        except Exception:
            # Sem supervisor a captura ainda funciona, so nao se recupera sozinha.
            log.exception("mic supervisor thread did not start")

    # ---------------------------------------------------------------- eventos
    def _emit(self, name, /, **kw):
        """Publica um evento. `name` e' POSICIONAL-ONLY de proposito.

        O payload de varios eventos carrega `name=` (o friendly name do endpoint) e
        com um parametro comum o Python recusava a chamada inteira com
        "got multiple values for argument 'name'" — na abertura do stream, ou seja
        em TODA abertura de microfone, e no detector de silencio digital, que e' o
        unico detector que existe para headset dormindo (ARCHITECTURE.md secao 4).
        A barra resolve aqui, de uma vez, sem depender de cada chamador lembrar.
        """
        payload = kw
        if "name" in payload:
            # O handler documentado e' `on_event(name, **kw)`: repassar um `name=`
            # no payload esbarraria de novo na mesma colisao, so que uma camada
            # adiante e engolida pelo except. A chave reservada e' renomeada, nao
            # jogada fora — o nome do endpoint e' o que explica um evento desses.
            payload = dict(kw)
            payload["device_name"] = payload.pop("name")
        log.log(_EVENT_LEVEL.get(name, logging.INFO), "audio %s %s", name, payload)
        try:
            self.on_event(name, **payload)
        except Exception:
            log.exception("on_event(%s) raised", name)

    def _emit_open(self):
        self._emit("open", device=self.device, name=self.name, hostapi=self.hostapi,
                   samplerate=self.sr, fallback=self.fallback, reopens=self.reopens)

    # ---------------------------------------------------------------- COM watcher
    def _register_watcher(self):
        if self._watcher is not None or self._stop.is_set():
            return
        if not _com_ready():
            # Sem MTA garantida a notificacao ou nao chega ou chega numa STA sem
            # bomba de mensagens; o supervisor de 0,5 s ainda pega a troca de headset.
            _com_degraded("_register_watcher")
            return
        watcher = None
        try:
            enum = enumerator()
            watcher = _Watcher(weakref.ref(self))
            # A ref forte entra ANTES do Register: depois dele o objeto ja pode ser
            # chamado de uma thread de RPC e nao pode depender de este Mic estar vivo.
            with _watchers_lock:
                _watchers.add(watcher)
            enum.RegisterEndpointNotificationCallback(watcher)
        except Exception as exc:
            # Register que falhou nao guardou ponteiro nenhum: solta, senao cada
            # tentativa de reabrir o microfone deixaria um COMObject para tras.
            if watcher is not None:
                with _watchers_lock:
                    _watchers.discard(watcher)
            log.warning("endpoint notification callback not registered: %r", exc)
            return
        # Guardar o enumerador que registrou: o cache global pode ser trocado se o
        # servico de audio reiniciar, e a MSDN exige desregistrar no mesmo objeto.
        self._watcher, self._watcher_enum = watcher, enum

    # ---------------------------------------------------------------- stream
    def _open(self):
        """Abre e inicia o stream. Chamar SEMPRE com o lock do PortAudio na mao.

        Nao emite evento: `open` sai em `_emit_open()`, fora do lock, para nao
        segurar o PortAudio enquanto um handler de bandeja qualquer roda.
        """
        self.device, self.name, self.fallback = resolve_device()
        info = sd.query_devices(self.device)
        try:
            self.hostapi = sd.query_hostapis(info["hostapi"])["name"]
        except Exception:
            self.hostapi = ""
        try:
            self.stream = sd.InputStream(device=self.device, samplerate=self.sr, channels=1,
                                         dtype="float32", blocksize=self.block, latency="low",
                                         callback=self._cb)
        except sd.PortAudioError as exc:
            # Headset Bluetooth em maos-livres (HFP) so abre na taxa nativa dele,
            # 16 kHz, e responde -9997 a qualquer outra. Medido: JBL TUNE125TWS,
            # WASAPI. Sem isto o mic ficava "dead" para sempre, reabrindo a cada
            # backoff com os mesmos 48 kHz.
            native = int(info.get("default_samplerate") or 0)
            if "sample rate" not in str(exc).lower() or native <= 0 or native == self.sr:
                raise
            log.warning("%s rejects %d Hz (%s); opening at its native %d Hz",
                        self.name, self.sr, exc, native)
            # O ring fica como esta: em amostras nao muda, em segundos cresce (16 kHz
            # triplica). mark() e to_whisper() leem self.sr ao vivo.
            # ponytail: taxa nativa ACIMA da configurada encurta o ring em segundos;
            # redimensionar aqui se aparecer um endpoint de 96 kHz.
            self.sr = native
            self.block = min(self.block, native)
            self.stream = sd.InputStream(device=self.device, samplerate=native, channels=1,
                                         dtype="float32", blocksize=self.block, latency="low",
                                         callback=self._cb)
        self.stream.start()
        self.last_error = ""

    def _close_stream(self, abort: bool = False) -> None:
        stream = self.stream
        if stream is None:
            return
        with _portaudio(PA_LOCK_TIMEOUT) as got:
            if not got:
                # Fechar sem o lock seria um Pa_CloseStream correndo contra o
                # Pa_Terminate/Pa_Initialize que o _reopen segura: access violation,
                # e sob pythonw.exe o processo morre calado. O stream fica INTACTO de
                # proposito — quem detem o lock e' o supervisor, e o close() ja setou
                # _stop, entao o proprio `if self._stop.is_set()` do _reopen fecha na
                # thread que e' dona do lock.
                log.warning("PortAudio busy; leaving the stream to the supervisor "
                            "(abort=%s)", abort)
                return
            # So desencosta o atributo depois de ter o lock, e so se ninguem tiver
            # trocado o stream no meio tempo (uma reabertura bem-sucedida troca).
            if self.stream is stream:
                self.stream = None
            try:
                if abort:
                    stream.abort()
                else:
                    stream.stop()
                stream.close()
            except Exception as exc:               # -9988 depois de um _terminate(): esperado
                log.debug("stream close failed: %r", exc)

    def _cb(self, indata, frames, t, status):
        """Callback do PortAudio. Zero I/O aqui: excecao derruba o stream sem aviso."""
        try:
            if status:
                self.xruns.append((time.monotonic(), str(status)))
            blk = indata[:, 0]
            size = self.ring.size
            if frames >= size:                     # ring minusculo: fica so com o final
                tail = blk[frames - size:]         # mantem o alinhamento written % size
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
            self.written += frames                 # publica DEPOIS que o dado entrou
        except Exception as exc:                   # o supervisor reporta; aqui nao se loga
            self._cb_err = repr(exc)[:160]

    # ---------------------------------------------------------------- gravacao
    def mark(self) -> int:
        """Marca o inicio da gravacao ja rebobinado pelo pre-roll.

        Sem esse recuo a primeira silaba some: entre a tecla e o `mark()` o usuario
        ja comecou a falar.
        """
        return max(0, self.written - int(self.sr * self.preroll_sec))

    def _slice(self, end: int, n: int) -> np.ndarray:
        size = self.ring.size
        s = (end - n) % size
        if s + n <= size:
            return self.ring[s:s + n].copy()
        return np.concatenate([self.ring[s:], self.ring[:(s + n) - size]])

    def take(self, start: int):
        """Recorta o audio 48k gravado desde `mark()`. Devolve (audio, status).

        status: 'ok' | 'lapped' (o ring deu a volta, o comeco se perdeu) | 'empty'.
        'empty' TAMBEM cobre silencio digital, e nesse caso o array volta vazio de
        proposito: o gate nao pode ser pulado por quem so olha `audio.size`. O audio
        cru fica em `self.last_audio` para diagnostico.
        """
        end = self.written
        size = self.ring.size
        # getattr com default: take() carrega o ditado inteiro e tem que ser total —
        # um AttributeError aqui vira ditado perdido sem rastro sob pythonw, e e' o
        # que deixa a conta do ring testavel sem abrir stream (tests/test_ring.py).
        blk = int(getattr(self, "block", BLOCK) or BLOCK)
        sr = int(getattr(self, "sr", CAPTURE_SR) or CAPTURE_SR)

        # Faixa de guarda de um bloco: o callback escreve no ring enquanto a gente
        # copia. Ler o ring inteiro encostaria justamente na emenda que ele esta
        # sobrescrevendo agora, e o inicio do audio sairia picotado.
        room = size - blk if size > 4 * blk else size
        n = int(min(end - start, room))
        self.last_audio = np.zeros(0, np.float32)
        self.last_rms = 0.0
        if n <= 0:
            return np.zeros(0, np.float32), "empty"

        out = self._slice(end, n)
        self.last_audio = out
        self.last_rms = rms(out)
        seconds = round(n / float(sr), 2)

        # GATE OBRIGATORIO. Headset sem fio dormindo devolve todas as amostras
        # exatamente 0.0 com state=Active, nao mudo, volume 0,85, stream.active True,
        # callbacks no horario, zero xruns e o IAudioMeterInformation lendo 0.0.
        # has_signal() no audio devolvido e' o UNICO detector que existe.
        if not self.has_signal(out, getattr(self, "silence_rms", None)):
            # O gate nunca e' pulado; so o AVISO e' limitado a 1/s, porque qualquer
            # medidor de nivel poderia chamar take() varias vezes por segundo.
            now = time.monotonic()
            if now - getattr(self, "_silent_t", 0.0) >= SILENT_EVENT_MIN_S:
                self._silent_t = now
                self._emit("silent_input", rms=self.last_rms, seconds=seconds,
                           device=getattr(self, "device", -1), name=getattr(self, "name", ""))
            return np.zeros(0, np.float32), "empty"

        # Pediu mais do que coube: o comeco foi por cima. O app avisa o usuario.
        if (end - start) > n:
            self._emit("lapped", seconds=seconds, ring_sec=round(size / float(sr), 2))
            return out, "lapped"
        return out, "ok"

    def tail(self, seconds: float = 0.1) -> np.ndarray:
        """Ultimas `seconds` do ring sem consumir nada — para o nivel do overlay."""
        try:
            n = int(max(0.0, float(seconds)) * self.sr)
        except (TypeError, ValueError):
            return np.zeros(0, np.float32)
        end = self.written
        n = min(n, self.ring.size, end)
        if n <= 0:
            return np.zeros(0, np.float32)
        return self._slice(end, n)

    def level(self, seconds: float = 0.1) -> float:
        """RMS instantaneo, pronto para `Overlay.set_level()`."""
        return rms(self.tail(seconds))

    @staticmethod
    def has_signal(x, threshold: float | None = None) -> bool:
        """Gate de RMS contra silencio digital. OBRIGATORIO por gravacao.

        Nenhuma API do Windows reporta esse defeito: state=Active, nao mudo,
        volume 0,85, stream.active True, callbacks no horario, zero xruns e ate o
        IAudioMeterInformation lendo pico 0.0. So o RMS do audio devolvido detecta.
        """
        try:
            thr = _gate_threshold() if threshold is None else float(threshold)
        except (TypeError, ValueError):
            thr = SILENCE_RMS
        if not math.isfinite(thr) or thr <= 0.0:
            thr = 1e-12                            # limiar nunca zera: silencio digital e' RMS 0.0
        return int(getattr(x, "size", 0)) > 0 and rms(x) > thr

    # ---------------------------------------------------------------- supervisor
    def _supervise(self):
        """0,5 s de ronda em stream.active E avanco de frames.

        Uma abertura WASAPI em modo exclusivo que FALHA em outro processo derruba o
        nosso stream compartilhado sem nenhuma flag de status: active vira False e/ou
        os frames simplesmente param. Recuperacao medida: 0,75 s.
        """
        _co_init()
        last = self.written
        fails = 0
        while not self._stop.wait(self.poll_sec):
            if self._cb_err:
                err, self._cb_err = self._cb_err, ""
                log.error("audio callback error: %s", err)

            # unknown=True: lock ocupado quer dizer que alguem esta mexendo no
            # stream agora, nao que ele morreu — reabrir por cima seria pior.
            alive = _stream_active(self.stream, unknown=True)

            if self._retarget.is_set():
                self._retarget.clear()
                try:
                    idx, name, _fb = resolve_device()
                except Exception:
                    idx, name = self.device, self.name
                # Comparar tambem pelo NOME: os indices do PortAudio sao renumerados a
                # cada _terminate()/_initialize(), entao o endpoint novo pode cair
                # exatamente no indice que o antigo ocupava.
                if idx != self.device or name != self.name:
                    self._emit("device_changed", old=self.device, old_name=self.name,
                               new=idx, new_name=name)
                    ok = self._reopen()
                    fails = 0 if ok else fails + 1
                    self._dead = not ok
                    if not ok:
                        fails = self._wait_backoff(fails)
                    last = self.written
                    continue

            stalled = (self.written == last) or not alive
            last = self.written
            if not stalled:
                fails = 0
                self._dead = False
                continue

            if not self._dead:
                self._dead = True
                self._emit("stream_dead", active=alive, reopens=self.reopens,
                           err=self.last_error)
            if self._reopen():
                fails = 0
                self._dead = False
            else:
                fails = self._wait_backoff(fails + 1)
            last = self.written

    def _wait_backoff(self, fails: int) -> int:
        """Backoff exponencial ate ~5 s: device removido de vez nao pode virar spin."""
        delay = min(BACKOFF_BASE * (2 ** max(0, min(fails, 32) - 1)), self.backoff_max)
        # Com o teto de 5 s isso repetiria ~720 vezes por hora com o mic desconectado:
        # avisa as 3 primeiras e depois so uma vez por minuto, para nao girar o log.
        if fails <= 3 or fails % 12 == 0:
            self._emit("reopen_failed", attempt=fails, retry_in=round(delay, 2),
                       err=self.last_error)
        self._stop.wait(delay)
        return fails

    def _reopen(self) -> bool:
        self.reopen_attempts += 1
        self._close_stream(abort=True)
        if self._stop.is_set():
            return False

        ok = False
        with _portaudio():
            # Se o _close_stream acima desistiu do lock, o stream velho continua
            # aberto. Agora o lock e' nosso e o RLock e' reentrante, entao esta
            # chamada fecha de verdade — Pa_Terminate por cima de um stream aberto
            # e' exatamente o crash que o timeout evitou la' atras.
            self._close_stream(abort=True)
            # Reciclar o PortAudio inteiro e' a unica forma de refrescar a lista de
            # devices em cache dele depois que o endpoint sumiu ou mudou.
            # terminate e initialize em blocos SEPARADOS de proposito: juntos, um
            # Pa_Terminate que falha leva o Pa_Initialize junto e o PortAudio fica
            # desinicializado para sempre — o microfone morre em silencio, e sob
            # pythonw ninguem ve.
            try:
                sd._terminate()
            except Exception as exc:
                log.debug("Pa_Terminate failed: %r", exc)
            try:
                sd._initialize()
            except Exception as exc:
                self.last_error = str(exc)[:160]
                log.warning("Pa_Initialize failed: %r", exc)
            else:
                try:
                    self._open()
                    ok = True
                except Exception as exc:
                    self.last_error = str(exc)[:160]
                    log.warning("reopen failed: %r", exc)
        if not ok:
            return False
        if self._stop.is_set():
            # close() correu junto com a reabertura: um stream aberto depois do
            # shutdown seguraria o microfone para sempre.
            self._close_stream(abort=True)
            return False
        self.reopens += 1
        self._emit_open()
        return True

    # ---------------------------------------------------------------- diagnostico
    def report(self) -> dict:
        """Diagnostico do menu do tray."""
        return {"name": self.name, "device": self.device, "hostapi": self.hostapi,
                "samplerate": self.sr, "reopens": self.reopens,
                "active": _stream_active(self.stream),
                "fallback": self.fallback, "blocksize": self.block,
                "ring_sec": self.ring_sec, "preroll_sec": self.preroll_sec,
                "silence_rms": self.silence_rms, "xruns": len(self.xruns),
                "attempts": self.reopen_attempts,
                "last_rms": round(self.last_rms, 6), "error": self.last_error}

    def close(self) -> None:
        self._stop.set()
        watcher, enum = self._watcher, self._watcher_enum
        self._watcher = self._watcher_enum = None
        if watcher is not None and enum is not None:
            try:
                enum.UnregisterEndpointNotificationCallback(watcher)
            except Exception as exc:
                # Falhou o Unregister: o COM ainda pode ter o ponteiro, entao o
                # objeto FICA vivo no registro global. Vazar alguns bytes e' o preco
                # de nao arriscar uma chamada num vtable liberado.
                log.debug("unregister notification callback failed: %r", exc)
            else:
                with _watchers_lock:
                    _watchers.discard(watcher)
        self._close_stream()
        global _active_mic
        if _active_mic is not None and _active_mic() is self:
            _active_mic = None


def device_report(cfg=None) -> dict:
    """Endpoint resolvido, indice sounddevice, host API, samplerate e reopens.

    Usa o Mic vivo se houver; senao resolve pelo COM so para exibir, sem abrir stream.
    Nunca levanta: e' item de menu, nao caminho quente.
    """
    mic = _live_mic()
    if mic is not None:
        try:
            return mic.report()
        except Exception as exc:                   # pragma: no cover
            return {"name": None, "device": None, "hostapi": None, "samplerate": None,
                    "reopens": 0, "active": False, "fallback": False,
                    "error": str(exc)[:160]}
    cfg = cfg or _dsp_cfg()
    out = {"name": None, "device": None, "hostapi": None,
           "samplerate": int(_num(cfg, "capture_sr", CAPTURE_SR, int,
                                  minimum=8000, maximum=MAX_CAPTURE_SR)),
           "reopens": 0, "active": False, "fallback": False, "error": None}
    try:
        # timeout: a bandeja roda na thread principal e nao pode ficar presa atras
        # de um _open() pendurado.
        with _portaudio(PA_LOCK_TIMEOUT) as got:
            if not got:
                raise RuntimeError("PortAudio ocupado")
            idx, name, fallback = resolve_device()
            info = sd.query_devices(idx)
            out.update(name=name, device=idx, fallback=fallback,
                       hostapi=sd.query_hostapis(info["hostapi"])["name"])
    except Exception as exc:
        out["error"] = str(exc)[:160]
    return out


# --------------------------------------------------------------------- DSP
_CFG = None
_HPF_CACHE = {}


def _dsp_cfg():
    """cfg do DSP.

    Prefere o cfg do Mic vivo: `to_whisper()` e' chamado sem argumento nenhum e um
    `capture_sr` diferente do que o stream realmente abriu daria resample na taxa
    errada — voz no tom errado, sem erro nenhum no log.
    """
    mic = _live_mic()
    cfg = getattr(mic, "cfg", None)
    if isinstance(cfg, dict) and cfg:
        return cfg
    global _CFG
    if _CFG is None:
        try:
            _CFG = config.load()
        except Exception:
            _CFG = config.Config(dict(config.DEFAULTS))
    return _CFG


def _live_capture_sr():
    """Taxa que o stream REALMENTE abriu, quando ha um Mic vivo."""
    sr = getattr(_live_mic(), "sr", None)
    return sr if isinstance(sr, int) and sr > 0 else None


def _highpass(sr: int, hz: float):
    """Butterworth 4a ordem passa-alta, em cache por (sr, hz)."""
    key = (int(sr), float(hz))
    if key not in _HPF_CACHE:
        if not (0 < hz < sr / 2.0):
            _HPF_CACHE[key] = None
        else:
            try:
                _HPF_CACHE[key] = butter(4, hz / (sr / 2.0), btype="highpass", output="sos")
            except Exception as exc:
                log.warning("highpass design failed for %s: %r", key, exc)
                _HPF_CACHE[key] = None
    return _HPF_CACHE[key]


_HPF = _highpass(TARGET_SR, HIGHPASS_HZ)           # pre-aquece o caso padrao 16k/80 Hz


def _resample_fallback(x, src: int, dst: int):
    """Emergencia, so se o soxr explodir: interpolacao linear, com alias. Melhor um
    ditado pior do que um ditado perdido."""
    n = int(round(x.size * (dst / float(src))))
    if n <= 0:
        return np.zeros(0, np.float32)
    idx = np.linspace(0.0, x.size - 1.0, n, dtype=np.float64)
    return np.interp(idx, np.arange(x.size, dtype=np.float64), x).astype(np.float32)


def to_whisper(x48, cfg=None) -> np.ndarray:
    """48k float32 -> 16k float32 pronto para o Whisper.

    1) soxr HQ 48k->16k: 0,68 ms por 5 s e rejeicao de alias de -156 dB, contra
       -73,6 dB do scipy.resample_poly.
    2) passa-alta Butterworth 4a ordem em 80 Hz via sosfilt: mata o bloco DC, que
       carrega ~5,7% da energia, e so 0,08% da energia da fala vive em 80-120 Hz.
       Causal esta correto de proposito: o log-mel do Whisper e' so magnitude, ignora fase.
    3) normalizacao de pico para -3 dBFS GUARDADA por `if peak > 1e-5`, para nunca
       amplificar silencio puro a fundo de escala. O Whisper nao e' invariante a
       ganho: as features deslocam 0,5 por decada; e clipar destroi as features.
    """
    explicit = cfg is not None
    cfg = cfg or _dsp_cfg()
    src = int(_num(cfg, "capture_sr", CAPTURE_SR, int, minimum=8000, maximum=MAX_CAPTURE_SR))
    if not explicit:
        src = _live_capture_sr() or src
    dst = int(_num(cfg, "target_sr", TARGET_SR, int, minimum=8000, maximum=MAX_CAPTURE_SR))
    hz = _num(cfg, "highpass_hz", HIGHPASS_HZ, float, minimum=0.0)
    # maximum=0.0: normalizar acima de 0 dBFS clipa, e o contrato do stt pede o audio
    # dentro de [-1,1]. Valor positivo no config.json e' preso em 0 dBFS (pico exato
    # em 1.0, ainda sem clipar), nao descartado.
    dbfs = _num(cfg, "normalize_dbfs", NORMALIZE_DBFS, float, maximum=0.0)

    x = np.ascontiguousarray(np.asarray(x48, dtype=np.float32).reshape(-1))
    if x.size == 0:
        return np.zeros(0, np.float32)

    # Limpar NaN/Inf ANTES do filtro: o sosfilt e' IIR, entao uma unica amostra
    # envenenada contamina o estado e zera o ditado inteiro da amostra em diante.
    if not bool(np.isfinite(x).all()):
        log.warning("non-finite samples from the driver; scrubbing before DSP")
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)

    if src != dst:
        try:
            y = soxr.resample(x, src, dst, quality="HQ")
        except Exception as exc:
            log.error("soxr resample failed (%r); using linear fallback", exc)
            y = _resample_fallback(x, src, dst)
    else:
        y = x.copy()

    sos = _highpass(dst, hz)
    if sos is not None:
        try:
            y = sosfilt(sos, y)
        except Exception as exc:
            log.error("highpass failed (%r); shipping unfiltered audio", exc)
    y = np.asarray(y, dtype=np.float32)

    peak = float(np.abs(y).max()) if y.size else 0.0
    if not math.isfinite(peak):                    # cinto e suspensorio depois do DSP
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        peak = float(np.abs(y).max()) if y.size else 0.0
    if peak > 1e-5:
        y = y * ((10.0 ** (dbfs / 20.0)) / peak)
    return np.ascontiguousarray(y, dtype=np.float32)
