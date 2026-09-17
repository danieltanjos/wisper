# -*- coding: utf-8 -*-
"""Integrador do wisper: boot, maquina de estados e desligamento.

Este modulo e o unico que conhece todos os outros. Ele se guia pelo que esta
escrito em docs/CONTRACT.md -- inclusive `Mic.stream`, `Mic.sr`, `Mic.last_audio`,
`Mic.level()/tail()` e `Mic.report()`, que estao documentados la justamente
porque este arquivo depende de todos eles.

Existe UMA excecao consciente, e ela esta aqui declarada em vez de escondida:
`_mic_alive()` chama `audio._stream_active()`, que e' privado. Ler `stream.active`
daqui seria um `Pa_IsStreamActive` num ponteiro que o supervisor do Mic pode estar
liberando no mesmo instante -- crash de processo, invisivel sob pythonw
(ARCHITECTURE.md secao 4). Essa funcao privada e' a mesma leitura serializada pelo
lock do PortAudio, e e' lida com `getattr`, com `Mic.report()` como alternativa
publica: um audio.py sem ela degrada, nao quebra.

Fluxo: idle -> recording -> transcribing -> injecting -> idle.
`Esc` volta para idle sem injetar nada, em QUALQUER um desses estados.

Regra de ouro deste arquivo: o processo roda sob pythonw.exe, sem console. Uma
excecao nao tratada e invisivel para o usuario. Todo caminho de falha registra no
log, avisa na pilula em portugues e degrada -- nunca derruba o app.

Duas flags do HotkeyEngine sao propriedade deste arquivo, e elas decidem quais
teclas o hook engole do sistema INTEIRO:

  `hotkey.recording`   -- so o `on_start` liga, e so depois do microfone de pe;
                          governa o Enter. Quem termina uma gravacao por conta
                          propria (timeout de max_record_sec, microfone caido,
                          pausa pela bandeja) tem que desligar: o engine so limpa
                          o que ele mesmo produziu.
  `hotkey.cancellable` -- o engine liga ao entregar o "start" e NUNCA desliga;
                          governa o Esc e sobrevive ao Enter de proposito, porque
                          o ditado so acaba quando o texto foi injetado. Quem
                          limpa e' este arquivo, em todo caminho que volta para
                          idle (`_clear_hotkey_flags`).

Flag presa = tecla sumida no resto do Windows. E' o pior defeito que este arquivo
pode ter, e por isso cada saida de estado passa por `_clear_hotkey_flags()`.
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import queue
import signal
import sys
import threading
import time
import traceback
import wave
from ctypes import wintypes
from datetime import datetime

import numpy as np

from wispr import config
from wispr import logging_setup

__version__ = "1.0.0"

# --- mensagens visiveis ao usuario (pt-BR, sem acento por consistencia com a pilula) ---
MSG_NO_SIGNAL = "Microfone mudo. Verifique o headset."
MSG_EMPTY = "Nao entendi nada."
MSG_MODEL_LOADING = "Carregando o modelo, tente de novo em instantes."
MSG_STT_ERROR = "Erro na transcricao (ver logs)."
MSG_ELEVATED = "Janela em foco e de administrador; o texto nao pode ser inserido."
MSG_ELEVATED_CLIP = MSG_ELEVATED + " Copiei para a area de transferencia: use Ctrl+V."
MSG_MIC_DEAD = "Microfone indisponivel. Reconecte o headset e tente de novo."
MSG_INJECT_FAIL = ("Nao consegui inserir o texto. Copiei para a area de transferencia: "
                   "use Ctrl+V.")
# Entrega parcial: nao existe "copiei para o clipboard" aqui de proposito. O
# comeco do texto JA esta na janela; colar o texto inteiro por cima duplicaria
# esse pedaco, e o usuario nao teria como saber que houve duas passadas.
MSG_INJECT_PARTIAL = ("Inseri so parte do texto. Nao repeti o resto para nao duplicar o "
                      "que ja entrou: o texto inteiro esta em 'Colar ultima transcricao'.")
MSG_NOTHING = "Nada para inserir."
MSG_CLIP_IMAGE = "A imagem que estava na area de transferencia se perdeu."
MSG_HOOK_DEAD = "Atalho nao instalado (ver logs)."
MSG_HOOK_LOST = "Atalho parou de funcionar (ver logs)."
MSG_DISABLED = "Ditado desligado. Ligue pelo icone na bandeja."
MSG_BUSY = "Ainda processando o ditado anterior."
MSG_CANCELLED = "Cancelado."
MSG_LAPPED = "Gravacao longa demais: o comeco do audio se perdeu."
MSG_MAXREC = "Tempo maximo de %ds atingido: encerrei a gravacao sozinho."
MSG_MIC_LOST = "Microfone caiu durante a gravacao."
MSG_VRAM_CPU = "Sem VRAM para a GPU; troquei para CPU (mais lento)."
MSG_STALLED = "O ditado travou e foi descartado (ver logs)."
MSG_NO_HISTORY = "Nada para repetir ainda."
MSG_ENABLED_ON = "Ditado ligado."
MSG_ENABLED_OFF = "Ditado desligado."

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

LEVEL_INTERVAL = 0.10        # ~10 atualizacoes de barrinha por segundo
LEVEL_WINDOW_S = 0.15        # janela de RMS mostrada na pilula
ENGINE_RETRY_S = 20.0        # nao insistir num modelo que acabou de falhar
ERROR_ICON_S = 2.5           # quanto tempo o icone fica vermelho depois de um erro
MIC_RETRY_S = 5.0            # cooldown para tentar reabrir o microfone morto
MSG_MAX_CHARS = 200          # a pilula corta em 200; mensagem de erro do stt vem de fora
STALL_CHECK_S = 60.0         # transcricao sem job em voo por tanto tempo = maquina travada
MIC_LOST_RATIO = 0.5         # audio abaixo disso do tempo de relogio = stream caiu no meio
MIC_LOST_MIN_S = 1.5         # abaixo disso a conta e' ruido: um toque rapido nao prova nada
MIC_FAULT_MSG_S = 3.0        # reopen_failed repete em backoff; a pilula nao pode piscar junto
PRELOAD_JOIN_S = 5.0         # espera do preload no shutdown (ele mesmo fecha o que criar)
# Teto da espera do run() por um shutdown que comecou em outra thread (a bandeja).
# Orcamento somado dos passos: hotkey ~4 s + bandeja + preload 5 s + PortAudio 2 s +
# motor 5 s + overlay 2 s. Passar disso, o processo sai e o SO recolhe o resto.
SHUTDOWN_WAIT_S = 20.0

_STATE_IDLE = "idle"
_STATE_RECORDING = "recording"
_STATE_TRANSCRIBING = "transcribing"
_STATE_INJECTING = "injecting"

# Modos que `_deliver()` devolve alem do "type"/"paste" do inject.
_MODE_CLIPBOARD = "clipboard"   # nada foi entregue; o texto esta no clipboard
_MODE_PARTIAL = "partial"       # entregou um pedaco e parou; repetir duplicaria
_MODE_EMPTY = "empty"           # nao havia texto depois do saneamento do inject
# Estes tres ja deixaram um aviso na pilula, e quem chama NAO pode chamar
# `overlay.hide()` depois: o hide zera o `msg_until` do overlay e o usuario nunca
# chegaria a ler a mensagem.
_MODES_WITH_MESSAGE = (_MODE_CLIPBOARD, _MODE_PARTIAL, _MODE_EMPTY)
# Estes dois sao falha de entrega: o icone fica vermelho ate o timer do tray.
_MODES_DEGRADED = (_MODE_CLIPBOARD, _MODE_PARTIAL)

# ---------------------------------------------------------------------------
# Win32: instancia unica e deteccao de janela elevada
# ---------------------------------------------------------------------------
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

# restype explicito e obrigatorio: sem isso o ctypes assume c_int e HANDLE de 64
# bits volta truncado. O mesmo bug que fazia SetWindowsHookExW falhar com 126.
_k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.GetCurrentProcess.argtypes = ()
_k32.GetCurrentProcess.restype = wintypes.HANDLE

_u32.GetForegroundWindow.argtypes = ()
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD

_adv.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
)
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.GetSidSubAuthorityCount.argtypes = (wintypes.LPVOID,)
_adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_adv.GetSidSubAuthority.argtypes = (wintypes.LPVOID, wintypes.DWORD)
_adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
SECURITY_MANDATORY_HIGH_RID = 0x3000


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def _integrity_level(hproc) -> int | None:
    """Nivel de integridade de um processo, ou None se nao der para ler."""
    tok = wintypes.HANDLE()
    if not _adv.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        size = wintypes.DWORD(0)
        _adv.GetTokenInformation(tok, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(int(size.value), 64))
        if not _adv.GetTokenInformation(tok, TOKEN_INTEGRITY_LEVEL, buf,
                                        len(buf), ctypes.byref(size)):
            return None
        label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
        count = _adv.GetSidSubAuthorityCount(label.Label.Sid)
        if not count:
            return None
        sub = _adv.GetSidSubAuthority(label.Label.Sid, wintypes.DWORD(count[0] - 1))
        return int(sub[0])
    except Exception:
        return None
    finally:
        _k32.CloseHandle(tok)


_own_il_cache: list = []


def _own_integrity_level() -> int | None:
    if not _own_il_cache:
        _own_il_cache.append(_integrity_level(_k32.GetCurrentProcess()))
    return _own_il_cache[0]


def process_is_elevated() -> bool:
    lvl = _own_integrity_level()
    return lvl is not None and lvl >= SECURITY_MANDATORY_HIGH_RID


def foreground_is_elevated() -> bool:
    """True quando a janela em foco roda acima do nosso nivel de integridade.

    Medido: dentro de janela elevada, SendInput e Ctrl+V sao descartados em
    silencio, com GetLastError() == 0 -- nao existe jeito de perceber depois.
    Entao a checagem tem que vir antes de injetar. Ver ARCHITECTURE.md secao 5.
    """
    try:
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return False
        hproc = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            # nem abrir o processo conseguimos: ele esta acima de nos, ponto.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            theirs = _integrity_level(hproc)
        finally:
            _k32.CloseHandle(hproc)
        ours = _own_integrity_level()
        if theirs is None or ours is None:
            return False
        return theirs > ours
    except Exception:
        return False


def log_environment(log: logging.Logger) -> None:
    """Fotografia do ambiente no boot. E a primeira coisa que se olha quando o
    usuario diz 'parou de funcionar'.

    Quem faz o despejo e o `logging_setup.log_environment()`, e so ele. A copia
    que existia aqui reprovava o Python da Microsoft Store olhando
    `sys.executable` -- que num venv e o python.exe do PROPRIO venv, enquanto o
    marcador da Store so aparece em `sys.base_prefix` -- e ainda dava falso
    positivo em qualquer caminho com "packages". Duas deteccoes discordando sobre
    o item mais grave do log (ARCHITECTURE.md secao 4) e pior que uma so.
    """
    try:
        logging_setup.log_environment(log)
    except Exception:
        # A funcao nao esta no CONTRACT.md: uma versao antiga do modulo nem a tem.
        log.exception("logging_setup.log_environment failed")
        log.info("wisper %s | python %s | pid %d | numpy %s", __version__,
                 sys.version.split()[0], os.getpid(), getattr(np, "__version__", "?"))


# ---------------------------------------------------------------------------
# Objetos de reserva: o app continua vivo mesmo sem pilula ou sem bandeja
# ---------------------------------------------------------------------------
class _NullOverlay:
    """Pilula que nao existe. Mantem as chamadas do app identicas."""

    def start(self):
        return self

    def show_recording(self):
        pass

    def show_transcribing(self):
        pass

    def show_message(self, text, ms=1800):
        pass

    def set_level(self, rms):
        pass

    def hide(self):
        pass

    def stop(self):
        pass


class _NullTray:
    """Bandeja que nao existe: sem icone, mas o hotkey continua funcionando e o
    processo nao morre. run() apenas segura a thread principal."""

    def __init__(self, app=None):
        self._evt = threading.Event()

    def run(self):
        self._evt.wait()

    def stop(self):
        self._evt.set()

    def set_state(self, state):
        pass

    def notify(self, title, msg):
        pass


class App:
    """Maquina de estados do wisper e dona do ciclo de vida de todo o resto."""

    def __init__(self, cfg=None):
        try:
            config.ensure_dirs()
        except OSError:
            # Disco cheio ou pasta somente leitura: o logging degrada sozinho e o
            # ditado ainda funciona. Morrer aqui seria morrer antes de haver log.
            pass
        if cfg is None:
            self.cfg = config.load()
        else:
            # Um dict cru (vindo de teste) nao tem acesso por atributo e derrubaria
            # o boot no primeiro self.cfg.log_level.
            self.cfg = cfg if isinstance(cfg, config.Config) else config.Config(cfg)
        # log provisorio ate logging_setup.setup() rodar em run(); nunca None.
        self.log: logging.Logger = logging.getLogger("wispr.app")
        self._root_log: logging.Logger | None = None

        self.enabled: bool = True
        self.last_text: str = ""
        self.history: list[str] = []
        self.version: str = __version__

        self.mic = None
        self.engine = None
        self.hotkey = None
        self.overlay = _NullOverlay()
        self.tray = _NullTray(self)

        self._lock = threading.RLock()
        self._eng_lock = threading.Lock()
        self._state = _STATE_IDLE
        self._session = 0
        self._cancelled = -1
        self._mark = None
        self._t_rec0 = 0.0
        self._stamp = None
        self._watchdog: threading.Timer | None = None
        self._stall: threading.Timer | None = None
        self._icon_timer: threading.Timer | None = None
        self._active_job: int | None = None   # sid que a worker esta processando agora
        self._auto_stop = (0, 0.0)            # (sid, segundos) do corte por max_record_sec

        self._engine_state = "idle"       # idle | loading | ready | error
        self._engine_err_t = 0.0
        self._force_cpu = False           # a GPU ja estourou nesta sessao: nao voltar
        self._mic_err_t = 0.0
        self._mic_feeds_level = False
        self._level_warned = False
        self._mic_fault = (0, 0.0)        # (sid, instante) do ultimo stream_dead/reopen_failed

        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._preload_thread: threading.Thread | None = None
        self._mutex = None
        self._closing = False
        self._shutdown_done = False
        self._shutdown_over = threading.Event()
        self._exit_code = 0

    # ------------------------------------------------------------------
    # propriedades de leitura, usadas pela bandeja
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def mic_ok(self) -> bool:
        return self._mic_alive(self.mic)

    @staticmethod
    def _mic_alive(mic) -> bool:
        """O Mic existe E o stream esta de pe.

        `Mic()` nao levanta quando o endpoint falha: ele nasce morto e deixa o
        supervisor reabrindo em backoff. Entao `self.mic is not None` nao quer
        dizer que existe captura acontecendo.

        Duas regras que este metodo nao pode quebrar:

        1. **Nunca** ler `stream.active` daqui. Isto roda na thread do hotkey (a
           cada Win+A), no boot e no fim da gravacao, enquanto a thread do
           supervisor pode estar dentro de `_reopen -> _close_stream ->
           stream.close()`. Um `Pa_IsStreamActive` num ponteiro recem-liberado e'
           crash de processo, e um `except Exception` nao pega crash nenhum. Quem
           serializa isso contra o PortAudio e' o `audio._stream_active()`
           (ARCHITECTURE.md secao 4); sem ele, `Mic.report()`, que faz o mesmo.
        2. Na duvida, **vivo**. Um Mic sem o atributo `stream` (outra versao do
           modulo, um duble) prendia o `mic_ok` em False para sempre, e ai todo
           Win+A respondia "Microfone indisponivel" com o microfone funcionando:
           exatamente o app morto em silencio que este arquivo existe para evitar.
           `stream = None` e' diferente disso -- e' o proprio audio.py dizendo que
           nao ha stream agora (reabrindo ou `_open()` falhou) e ai e' False mesmo.
        """
        if mic is None:
            return False
        if getattr(mic, "on_demand", False) and not getattr(mic, "_wanted", True):
            # Sob demanda e fora de um ditado: sem stream POR DESENHO. Vivo. Se o
            # endpoint falhar, e' o start() do proximo Win+A que levanta.
            return True
        unknown = object()
        try:
            stream = getattr(mic, "stream", unknown)
        except Exception:
            return True
        if stream is unknown:
            return True           # nao da para saber => tentar gravar, nao recusar
        if stream is None:
            return False          # _open() falhou, ou _reopen() esta no meio do caminho
        try:
            from wispr import audio as audio_mod
            check = getattr(audio_mod, "_stream_active", None)
            if check is not None:
                # unknown=True: o lock do PortAudio ocupado quer dizer "outra
                # thread esta mexendo no stream", nao "o microfone morreu".
                return bool(check(stream, unknown=True))
            report = getattr(mic, "report", None)
            if callable(report):
                # Medido: uma abertura WASAPI exclusiva que falha em OUTRO processo
                # derruba o nosso stream compartilhado e 'active' vira False sem
                # nenhuma flag de status. Ver ARCHITECTURE.md secao 4.
                active = report().get("active")
                return True if active is None else bool(active)
        except Exception:
            pass
        return True               # nao respondeu: assumir vivo e tentar gravar

    @property
    def engine_status(self) -> str:
        return self._engine_state

    def hook_alive(self):
        """True/False conforme o `WH_KEYBOARD_LL` esta instalado, None se nao da
        para saber.

        None NUNCA pode virar "quebrado": durante o boot o engine ainda nem
        existe, e uma versao antiga dele nao tem a propriedade `alive`. Sem hook
        nao existe Win+A -- o app ficaria verde de "pronto" com o usuario
        apertando a tecla no vazio (ARCHITECTURE.md secao 3).
        """
        hk = self.hotkey
        if hk is None:
            # `_start_hotkey` sempre atribui; None so sobra quando ele falhou, e
            # a bandeja so nasce depois dele. Isso e' um fato, nao um "nao sei".
            return False
        try:
            alive = getattr(hk, "alive", None)
            if callable(alive):
                alive = alive()          # tolera `alive()` em vez de propriedade
            if alive is None:
                installs = int(getattr(hk, "installs", 0) or 0)
                return installs > 0
            return bool(alive)
        except Exception:
            self.log.debug("hotkey health probe failed", exc_info=True)
            return None

    def status_text(self) -> str:
        """Uma linha em pt-BR para o tooltip/menu da bandeja."""
        if not self.enabled:
            return "Ditado desligado"
        if self.hook_alive() is False:
            # Sem hook nada mais importa: o Win+A nem chega ate' aqui.
            return "Atalho inativo (ver logs)"
        if self.mic is None:
            return "Microfone indisponivel"
        if self._engine_state == "loading":
            return "Carregando o modelo..."
        if self._engine_state == "error":
            return "Modelo indisponivel (ver logs)"
        return {
            _STATE_IDLE: "Pronto",
            _STATE_RECORDING: "Gravando...",
            _STATE_TRANSCRIBING: "Transcrevendo...",
            _STATE_INJECTING: "Inserindo texto...",
        }.get(self._state, "Pronto")

    # ------------------------------------------------------------------
    # boot
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Sobe tudo na ordem certa e entra no loop da bandeja. Bloqueia."""
        # O mutex vem ANTES do logging: o handler rotativo mantem
        # logs/wisper.log aberto, e duas copias do app com o arquivo aberto ao
        # mesmo tempo fazem a rotacao falhar com PermissionError no Windows. A
        # segunda instancia, por isso, sai antes de abrir qualquer arquivo -- e
        # por isso tambem ela sai calada: nao ha log dela para ler.
        if not self._acquire_mutex():
            return 0
        try:
            self._root_log = logging_setup.setup(self.cfg.get("log_level", "INFO"))
            self.log = logging_setup.get("wispr.app")
        except Exception:
            # Sem log o app ainda dita. Morrer aqui seria morrer exatamente onde
            # ninguem consegue ler o motivo.
            self.log.exception("logging setup failed; continuing with the bare logger")
        log_environment(self.log)
        if config.load_error:
            # O aviso do config.load() saiu antes de o logging existir.
            self.log.warning("config.json ignored, using defaults: %s", config.load_error)

        atexit.register(self.shutdown)
        self._install_signal_handlers()

        self._start_overlay()
        self._start_mic(first=True)
        self._start_worker()

        if self.cfg.get("preload_model", True):
            # Carregar o modelo aqui na main travaria a bandeja por segundos; numa
            # thread daemon o icone aparece na hora e o modelo chega depois.
            # A referencia fica guardada porque o shutdown espera por ela: sair nos
            # primeiros segundos deixaria ~1 GB de VRAM e um contexto CUDA sendo
            # criados DEPOIS do "shutdown complete", sem ninguem para fechar.
            try:
                self._preload_thread = threading.Thread(
                    target=self._build_engine, args=("preload",), name="preload", daemon=True)
                self._preload_thread.start()
            except Exception:
                self._preload_thread = None
                self.log.exception("preload thread did not start; the model loads on the "
                                   "first dictation")

        self._start_hotkey()

        try:
            from wispr.tray import Tray
            self.tray = Tray(self)
        except Exception:
            self.log.exception("tray unavailable; running headless")
            self.tray = _NullTray(self)

        # Avisos que nasceram antes da bandeja existir so podem ser dados agora.
        hook_ok = self.hook_alive() is not False
        if not self.mic_ok:
            # Mic() nao levanta quando o endpoint falha: sem olhar o stream o app
            # anunciaria "pronto" com o microfone morto.
            self.log.error("no capture at boot (mic=%s)", "dead" if self.mic else "absent")
            self._tray_state("error")
            self._notify("wisper", MSG_MIC_DEAD)
        elif not hook_ok:
            # Sem hook instalado o app fica invisivel e mudo: o Win+A simplesmente
            # nao chega. O watchdog do hotkey ainda tenta reinstalar sozinho.
            self.log.error("keyboard hook not installed at boot (installs=%s failed=%s)",
                           getattr(self.hotkey, "installs", None),
                           getattr(self.hotkey, "failed_installs", None))
            self._tray_state("error")
            self._notify("wisper", MSG_HOOK_DEAD)
        else:
            self._tray_state(_STATE_IDLE)
        try:
            self.tray.run()   # bloqueia a thread principal; pystray tem bombeamento proprio
        except Exception:
            self.log.exception("tray loop crashed")
            self._exit_code = 1
        finally:
            self.shutdown()
            # A bandeja dispara o shutdown numa thread daemon e devolve o
            # `run()` na hora (senao o menu "Sair" segura o bombeamento da
            # thread principal por ate' ~13 s). Se a main retornasse agora, a
            # finalizacao do interpretador mataria essa thread dentro do
            # ctranslate2 ou do PortAudio -- crash duro, invisivel sob pythonw.
            # A espera e' aqui, no dono do processo, nunca no callback do menu.
            if not self._shutdown_over.wait(SHUTDOWN_WAIT_S):
                self.log.warning("shutdown still running after %.0fs; exiting anyway",
                                 SHUTDOWN_WAIT_S)
        return self._exit_code

    def _acquire_mutex(self) -> bool:
        """Instancia unica. Segunda copia sai calada, sem nenhuma janela modal.

        `bInitialOwner=False` de proposito: a existencia do mutex mais o
        ERROR_ALREADY_EXISTS ja respondem tudo que a checagem precisa, e com
        posse o `ReleaseMutex` so funciona na thread que criou -- de qualquer
        outra ele falha com ERROR_NOT_OWNER, que e' exatamente o caso agora que
        o shutdown roda numa thread da bandeja. Sem posse nao ha o que liberar:
        basta o CloseHandle.
        """
        try:
            handle = _k32.CreateMutexW(None, False, config.MUTEX_NAME)
            err = ctypes.get_last_error()
            if handle and err == ERROR_ALREADY_EXISTS:
                self.log.warning("another instance already owns %s (error %d); exiting quietly",
                                 config.MUTEX_NAME, ERROR_ALREADY_EXISTS)
                _k32.CloseHandle(handle)
                return False
            if not handle:
                self.log.error("CreateMutexW failed (error %d); continuing without "
                               "single-instance guard", err)
                return True
            self._mutex = handle
            return True
        except Exception:
            self.log.exception("single-instance check failed; continuing")
            return True

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self.log.info("signal %s received; shutting down", signum)
            self.shutdown()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass   # sob pythonw.exe nem todo sinal existe; atexit cobre o resto

    def _start_overlay(self) -> None:
        if not self.cfg.get("overlay", True):
            self.log.info("overlay disabled by config")
            return
        try:
            from wispr.overlay import Overlay
            self.overlay = Overlay(self.cfg).start()
            self.log.info("overlay up")
        except Exception:
            self.log.exception("overlay failed to start; continuing without it")
            self.overlay = _NullOverlay()

    def _start_mic(self, first: bool = False) -> bool:
        """Abre o microfone. Falhar aqui degrada o app, nunca o mata."""
        if self.mic is not None:
            return True
        if self._closing:
            return False
        if not first and (time.monotonic() - self._mic_err_t) < MIC_RETRY_S:
            return False
        try:
            # importar wispr.audio traz comtypes antes de sounddevice, na ordem
            # fixada em wispr/__init__.py. Nunca inverter. ARCHITECTURE.md secao 4.
            from wispr.audio import Mic
            mic = Mic(self.cfg, on_event=self._on_mic_event)
            if self._closing:
                # O shutdown ja passou por "mic": sem esta conferencia sobraria um
                # stream WASAPI aberto e a thread mic-sup viva, com o LED de mudo do
                # headset aceso depois de o app ter fechado.
                self.log.info("mic opened during shutdown; closing it right back")
                try:
                    mic.close()
                except Exception:
                    self.log.exception("could not close the late microphone")
                return False
            self.mic = mic
            self.log.info("mic ready: %s (device %s)", getattr(self.mic, "name", "?"),
                          getattr(self.mic, "device", "?"))
            return True
        except Exception:
            self._mic_err_t = time.monotonic()
            self.log.exception("microphone unavailable")
            # Aviso na bandeja fica para depois: no boot ela ainda nao existe.
            return False

    def _mic_idle(self) -> None:
        """Solta o microfone entre ditados (sob demanda). Nunca levanta."""
        stop = getattr(self.mic, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                self.log.exception("mic.stop() failed")

    def _start_hotkey(self) -> None:
        try:
            from wispr.hotkey import HotkeyEngine
            try:
                # `on_error` e' keyword-only e nao esta no CONTRACT.md: um engine
                # com a assinatura de tres callbacks continua servindo, so sem o
                # aviso de hook morto (o watchdog dele segue reinstalando).
                self.hotkey = HotkeyEngine(self.on_start, self.on_stop, self.on_cancel,
                                           self.cfg, on_error=self.on_hook_error)
            except TypeError:
                self.log.warning("hotkey engine has no on_error callback; sustained hook "
                                 "failures will only appear in the log")
                self.hotkey = HotkeyEngine(self.on_start, self.on_stop, self.on_cancel,
                                           self.cfg)
            self.hotkey.start()
            self.log.info("hotkey engine up (%s / stop=%s / cancel=%s)",
                          self.cfg.get("hotkey"), self.cfg.get("stop_key"),
                          self.cfg.get("cancel_key"))
        except Exception:
            self.hotkey = None
            self.log.exception("hotkey engine failed to start")
            self._notify("wisper", "Atalho indisponivel (ver logs).")

    def on_hook_error(self, reason: str) -> None:
        """O engine desistiu de reinstalar o hook (falhas seguidas).

        Chamada na thread worker do hotkey. Sem hook nao existe Win+A, e ate'
        aqui isso so aparecia no log -- o usuario ficava apertando a tecla
        achando que o ditado e' que estava ruim.
        """
        try:
            # A elevacao entra na linha porque e' a causa numero um: sob janela
            # elevada em foco o hook nao recebe evento nenhum, e rodar elevado e'
            # a unica correcao (ARCHITECTURE.md secao 3).
            self.log.error("keyboard hook is failing: %s (this process is %s)", reason,
                           "elevated" if process_is_elevated() else "not elevated")
            if self._closing:
                return
            self._message(MSG_HOOK_LOST, 4000)
            self._notify("wisper", MSG_HOOK_LOST)
            # Sem timer de volta ao verde: a bandeja le `hotkey.alive` a cada
            # repintura e mantem o vermelho enquanto o hook nao voltar.
            self._tray_state("error")
        except Exception:
            self.log.exception("on_hook_error failed")

    def _start_worker(self) -> bool:
        """Sobe a thread que transcreve e injeta. Sem ela nenhum ditado anda."""
        try:
            worker = threading.Thread(target=self._worker_loop, name="worker", daemon=True)
            worker.start()
            self._worker = worker
            return True
        except Exception:
            # E' o unico passo do boot que nao tinha protecao: um
            # "can't start new thread" aqui derrubava o app inteiro no lugar onde
            # nao existe console para ler o motivo.
            self._worker = None
            self.log.exception("worker thread did not start")
            return False

    def _worker_ok(self) -> bool:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return True
        # Uma tentativa nova: falta momentanea de memoria nao pode condenar a
        # sessao inteira. Falhando de novo, quem avisa e' o on_start -- deixar o
        # ditado seguir sem worker daria 60 s de pilula girando ate' o stall check.
        return self._start_worker()

    # ------------------------------------------------------------------
    # motor de transcricao
    # ------------------------------------------------------------------
    def _get_engine(self):
        """Engine pronta, ou None quando ainda carrega / falhou."""
        if self.engine is not None:
            return self.engine
        if self._engine_state == "loading":
            return None
        return self._build_engine("on-demand")

    def _engine_cfg(self):
        """A cfg com que o motor e' construido. Copia so quando precisa forcar CPU."""
        if not self._force_cpu:
            return self.cfg
        # Copia: mexer em self.cfg["device"] vazaria para o config.json na
        # primeira vez que a bandeja salvasse (ela salva a cfg do app inteira),
        # e o usuario ficaria preso na CPU para sempre por causa de um jogo
        # aberto uma tarde.
        cfg = config.Config(dict(self.cfg))
        cfg["device"] = "cpu"
        return cfg

    def _build_engine(self, reason: str):
        if self._closing:
            return None
        # acquire sem bloquear de verdade: se outra thread ja esta carregando, quem
        # chegou depois recebe None e o usuario ve "carregando", em vez de travar.
        if not self._eng_lock.acquire(timeout=0.05):
            return None
        try:
            if self.engine is not None:
                return self.engine
            if (self._engine_state == "error"
                    and (time.monotonic() - self._engine_err_t) < ENGINE_RETRY_S):
                return None
            self._engine_state = "loading"
            t0 = time.perf_counter()
            try:
                # wispr.stt roda _add_cuda_dll_dirs() antes de tocar em faster_whisper:
                # ctranslate2.dll carrega cublas64_12.dll dinamicamente e so estoura no
                # transcribe(). Nunca importar faster_whisper daqui.
                from wispr import stt
                engine = stt.Engine(self._engine_cfg())
            except Exception:
                self._engine_state = "error"
                self._engine_err_t = time.monotonic()
                self.log.exception("stt engine failed to load (reason=%s)", reason)
                return None
            if self._closing:
                # O carregamento leva segundos e o usuario pode ter saido no meio
                # deles: sem esta conferencia sobra ~1 GB de VRAM e um contexto CUDA
                # criados DEPOIS do "shutdown complete", e a finalizacao do
                # interpretador mata esta thread daemon dentro do ctranslate2.
                self._engine_state = "idle"
                self.log.info("stt engine finished loading during shutdown (%s); closing it",
                              reason)
                try:
                    engine.close()
                except Exception:
                    self.log.exception("could not close the late stt engine")
                return None
            self.engine = engine
            self._engine_state = "ready"
            self.log.info("stt ready (%s): backend=%s model=%s load=%.2fs warm=%.2fs wall=%.2fs",
                          reason, getattr(engine, "backend", "?"),
                          getattr(engine, "model_id", "?"),
                          float(getattr(engine, "load_s", 0.0) or 0.0),
                          float(getattr(engine, "warm_s", 0.0) or 0.0),
                          time.perf_counter() - t0)
            return engine
        finally:
            self._eng_lock.release()

    def _drop_engine(self, reason: str) -> None:
        """Solta o motor atual e deixa o proximo `_build_engine` livre para subir.

        Fecha FORA do lock: `Engine.close()` espera a transcricao em voo (ate' 5 s)
        e segurar o lock do app por esse tempo faria toda a bandeja parecer travada.
        """
        got = self._eng_lock.acquire(timeout=10.0)
        if not got:
            # Alguem esta carregando um modelo ha mais de 10 s. Seguir em frente e'
            # melhor que desistir: o motor novo so sera atribuido se `self.engine`
            # ainda estiver vago, e o antigo precisa ser fechado de qualquer jeito.
            self.log.warning("engine lock busy for 10s; dropping the engine anyway (%s)", reason)
        try:
            old, self.engine = self.engine, None
            self._engine_state = "idle"
            self._engine_err_t = 0.0     # nao punir a troca com o cooldown do erro anterior
        finally:
            if got:
                self._eng_lock.release()
        if old is None:
            return
        self.log.info("closing the stt engine (%s): backend=%s", reason,
                      getattr(old, "backend", "?"))
        try:
            old.close()
        except Exception:
            self.log.exception("stt engine close failed (%s)", reason)

    def _fallback_to_cpu(self, broken):
        """Reconstroi o motor na CPU depois de a GPU falhar NO transcribe().

        O fallback cuda->cpu do `stt.Engine` so existe no __init__: se o modelo
        carregou com o desktop ocioso e o usuario abriu um jogo depois, todo
        ditado passa a estourar e o `_get_engine()` devolve o mesmo motor
        quebrado a sessao inteira. Foram medidos apenas 1.746 MiB de VRAM livre
        com o desktop em uso (ARCHITECTURE.md secao 2), entao isto e' caminho
        esperado, nao acidente. Devolve o motor novo ou None.
        """
        if self._closing:
            return None
        if self.engine is not broken:
            # Outra thread ja trocou o motor debaixo deste ditado (a bandeja
            # mudando para a Groq, ou outro ditado que ja caiu para a CPU). Nao
            # existe GPU para condenar aqui: retenta com o que estiver de pe.
            return self.engine
        if str(getattr(broken, "backend", "")) != "cuda":
            return None        # ja estamos na CPU ou na Groq: nao ha para onde cair
        self._force_cpu = True
        self._drop_engine("cuda failed at transcribe time")
        engine = self._build_engine("cuda-oom")
        if engine is None:
            return None
        self.log.warning("stt fell back to cpu after a cuda failure: backend=%s",
                         getattr(engine, "backend", "?"))
        self._message(MSG_VRAM_CPU, 3200)
        self._notify("wisper", MSG_VRAM_CPU)
        return engine

    def set_engine(self, name: str) -> None:
        """Troca o motor a quente ("local" | "groq"). A bandeja chama isto.

        Nunca levanta: a bandeja roda numa thread daemon e trataria a excecao como
        "Falha em trocar para X", mas o processo nao pode depender disso. Uma
        troca que falha deixa o estado em "error", avisa o usuario e o proximo
        ditado tenta de novo.
        """
        try:
            name = "groq" if str(name).strip().lower() == "groq" else "local"
            # A comparacao e' com o BACKEND vivo, nunca com cfg["engine"]: a
            # bandeja grava a chave nova antes de chamar aqui, entao a cfg ja
            # concorda com `name` e uma checagem por ela nunca trocaria nada.
            live = str(getattr(self.engine, "backend", "") or "")
            if self.engine is not None and (live == "groq") == (name == "groq"):
                self.log.debug("engine already running as %r (backend=%s)", name, live)
                return
            self.log.info("switching stt engine to %r", name)
            self.cfg["engine"] = name        # a bandeja ja persistiu isso no config.json
            if name == "groq":
                # A Groq nao toca na GPU: a punicao por VRAM nao vale para ela, e
                # voltar para "local" depois merece uma chance nova na GPU.
                self._force_cpu = False
            self._drop_engine("engine switch to %s" % name)
            if self._build_engine("tray:%s" % name) is None:
                self.log.error("engine switch to %r did not produce a usable engine", name)
                self._notify("wisper", "Motor %s indisponivel (ver logs)." % name)
        except Exception:
            self.log.exception("set_engine(%r) failed", name)

    # ------------------------------------------------------------------
    # callbacks do hotkey engine (chamados na thread worker do engine)
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        # Quem liga `recording` e' SO este metodo, dentro do lock, depois de o
        # microfone estar de pe: entre a tecla e a captura existem 80-92 ms
        # medidos (ARCHITECTURE.md secao 4) e ligar antes faria o hook engolir um
        # Enter do usuario para uma gravacao que ainda nao existe.
        # `cancellable` ja vem ligada do engine (o Esc cancela desde o Win+A) e o
        # dono da limpeza dela e' este arquivo: toda recusa aqui desliga, senao o
        # Esc some no sistema inteiro para sempre.
        if self._closing:
            self._clear_hotkey_flags()
            return
        if not self.enabled:
            self._clear_hotkey_flags()
            self.ping_error()
            self._message(MSG_DISABLED)
            return
        if not self.mic_ok:
            # Com o stream caido o ring nao avanca e a gravacao sai vazia de
            # qualquer jeito: melhor avisar agora do que deixar o usuario falar
            # 20 s para o nada. O supervisor do Mic segue reabrindo por conta.
            self._start_mic()
            if not self.mic_ok:
                self._clear_hotkey_flags()
                self.ping_error()
                self._message(MSG_MIC_DEAD, 2600)
                return
        if not self._worker_ok():
            self._clear_hotkey_flags()
            self.ping_error()
            self._message(MSG_STT_ERROR, 2600)
            return

        with self._lock:
            if self._state != _STATE_IDLE:
                if self._state in (_STATE_TRANSCRIBING, _STATE_INJECTING):
                    self._message(MSG_BUSY)
                # Nao mexer nas flags: existe um ditado vivo (gravando ou sendo
                # transcrito) e ele continua tendo que responder a Enter e Esc.
                # Limpar aqui seria roubar o cancelamento do ditado do usuario.
                self.log.debug("start ignored, state=%s", self._state)
                return
            self._session += 1
            sid = self._session
            self._state = _STATE_RECORDING
            self._t_rec0 = time.perf_counter()
            self._stamp = datetime.now()   # nome do wav sai daqui, no inicio da fala
            self._auto_stop = (0, 0.0)
            self._mic_fault = (0, 0.0)
            self._set_hotkey_recording(True)

        try:
            start = getattr(self.mic, "start", None)
            if callable(start):
                start()                    # sob demanda: abre o stream agora
            self._mark = self.mic.mark()
        except Exception:
            self.log.exception("mic.start()/mark() failed")
            self._abort_recording(MSG_MIC_DEAD)
            return

        self.log.info("recording started (session %d)", sid)
        self.overlay.show_recording()
        self._tray_state(_STATE_RECORDING)
        self.ping_start()
        self._arm_watchdog(sid)
        try:
            threading.Thread(target=self._level_loop, args=(sid,), name="level",
                             daemon=True).start()
        except Exception:
            # Barrinha parada e cosmetico; abortar a gravacao por causa dela, nao.
            self.log.exception("could not start the level meter thread")

    def on_stop(self) -> None:
        with self._lock:
            if self._state != _STATE_RECORDING:
                self.log.debug("stop ignored, state=%s", self._state)
                return
            sid = self._session
            mark = self._mark
            stamp = self._stamp or datetime.now()
            rec_s = time.perf_counter() - self._t_rec0
            auto_sid, auto_s = self._auto_stop
            self._state = _STATE_TRANSCRIBING
            self._set_hotkey_recording(False)
        # Daqui para baixo TUDO tem que estar protegido. Uma excecao solta neste
        # trecho (o `ping_stop()` sobe uma thread, o `show_transcribing()` fala com
        # o Tk) escapava para o `HotkeyEngine._fire`, que so loga -- e o `_state`
        # ficava em 'transcribing' PARA SEMPRE: on_start responde "ocupado",
        # on_stop sai na porta, nenhum job foi enfileirado, e a bandeja segue
        # dizendo "Transcrevendo..." com o app morto por dentro.
        try:
            self._finish_recording(sid, mark, stamp, rec_s,
                                   auto_s if auto_sid == sid else 0.0)
        except Exception:
            self.log.exception("on_stop crashed after the state moved to transcribing")
            self._fail(MSG_STT_ERROR, sid=sid)

    def _finish_recording(self, sid: int, mark, stamp, rec_s: float, auto_s: float) -> None:
        self._disarm_watchdog()
        self._arm_stall_check(sid)
        self.ping_stop()
        self.overlay.show_transcribing()
        self._tray_state("working")

        mic = self.mic
        try:
            audio_48k, status = mic.take(mark)
        except Exception:
            self.log.exception("mic.take() failed")
            self._mic_idle()
            self._fail(MSG_MIC_DEAD, sid=sid)
            return
        # O audio ja esta copiado do ring: o microfone pode ser solto antes da
        # transcricao, que na CPU leva segundos. O "vivo?" e' lido ANTES de soltar,
        # senao um stream que morreu no meio da fala viraria "microfone mudo".
        mic_alive = self._mic_alive(mic)
        self._mic_idle()

        n = 0 if audio_48k is None else int(getattr(audio_48k, "size", 0))
        truncated = self._audio_is_truncated(mic, sid, n, rec_s)
        self.log.info("recording stopped (session %d): %.2fs, %d samples, status=%s, "
                      "truncated=%s auto_stop=%.0fs", sid, rec_s, n, status, truncated, auto_s)
        if status == "empty" or n == 0:
            # 'empty' cobre tres casos muito diferentes: o headset entregando
            # silencio digital (stream vivo), o stream caido esperando o supervisor
            # reabrir, e o stream que morreu NO MEIO da fala -- neste ultimo o
            # pre-roll ainda carrega ruido de sala, o gate passa e o usuario ouvia
            # "nao entendi nada", culpando a propria diccao em vez do headset.
            if not mic_alive:
                msg = MSG_MIC_DEAD
            elif truncated:
                msg = MSG_MIC_LOST
            else:
                msg = MSG_NO_SIGNAL
            if auto_s > 0:
                # A gravacao foi cortada por nos: sem isso o usuario le so
                # "microfone mudo" e nao sabe que o ditado tambem foi encerrado.
                msg = (MSG_MAXREC % int(auto_s)) + " " + msg
            self._fail(msg, sid=sid)
            return
        if status == "lapped":
            self.log.warning("ring buffer lapped: beginning of the audio was lost")
        self._jobs.put((sid, audio_48k, status, stamp, rec_s,
                        {"auto_stop": auto_s, "truncated": truncated}))

    def _audio_is_truncated(self, mic, sid: int, n: int, rec_s: float) -> bool:
        """O audio devolvido e' drasticamente mais curto que o relogio de parede?

        E' o unico jeito de distinguir "o usuario nao falou" de "o headset
        desligou no segundo 5 de uma fala de 40 s": o `written` congela, o
        `take()` devolve so o pre-roll, e esse pre-roll tem ruido de sala
        suficiente para o gate de RMS aprovar.
        """
        try:
            if rec_s < MIC_LOST_MIN_S:
                return False
            if self._mic_fault[0] == sid:
                return True      # stream_dead/reopen_failed durante ESTA gravacao
            sr = int(getattr(mic, "sr", 0) or 0) or int(self.cfg.get("capture_sr") or 48000)
            got = int(n)
            if got == 0:
                # Gate de silencio: o take() devolve vazio de proposito, e o audio
                # cru fica em last_audio -- e' dele que sai o tamanho real.
                got = int(getattr(getattr(mic, "last_audio", None), "size", 0) or 0)
            return got < rec_s * sr * MIC_LOST_RATIO
        except Exception:
            self.log.debug("truncation check failed", exc_info=True)
            return False

    def on_cancel(self) -> None:
        """`Esc`. Cancela tanto a gravacao quanto o ditado ja em transcricao.

        O engine entrega este evento enquanto `cancellable` estiver ligada, ou
        seja, do Win+A ate' o texto ser injetado -- e' isso que faz o CONTRACT.md
        valer ("Esc volta para idle sem injetar nada") tambem durante os segundos
        de transcricao.
        """
        with self._lock:
            state = self._state
            sid = self._session
            if state == _STATE_IDLE:
                # Sem ditado nenhum e o Esc chegou: a flag ficou presa. Limpar e'
                # o que devolve o Esc ao aplicativo em foco.
                self._clear_hotkey_flags()
                return
            self._cancelled = sid          # o worker confere isso antes de injetar
            self._state = _STATE_IDLE
            self._clear_hotkey_flags()
        self._disarm_timers()
        self.log.info("cancelled by user (session %d, state=%s)", sid, state)
        if state == _STATE_RECORDING:
            self._mic_idle()
            self.overlay.hide()
        else:
            # Transcricao em voo: o job continua rodando por alguns segundos e so
            # descobre o cancelamento na guarda de pre-injecao. A pilula nao pode
            # ficar girando o spinner nesse meio tempo.
            self._message(MSG_CANCELLED, 1200)
        self._tray_state(_STATE_IDLE)

    def _abort_recording(self, msg: str) -> None:
        """Sai de recording sem passar pelo worker."""
        self._disarm_timers()
        self._mic_idle()
        with self._lock:
            self._state = _STATE_IDLE
            self._clear_hotkey_flags()
        self._fail(msg)

    def _set_hotkey_recording(self, value: bool) -> None:
        # O engine engole o Enter so enquanto essa flag esta ligada; se ela ficar
        # presa, o Enter do usuario some no resto do sistema.
        try:
            if self.hotkey is not None:
                self.hotkey.recording = value
        except Exception:
            self.log.exception("failed to set hotkey.recording=%s", value)

    def _clear_hotkey_flags(self) -> None:
        """Fim de ditado: nem Enter nem Esc podem continuar sendo engolidos.

        `cancellable` e' ligada pelo engine e NUNCA desligada por ele -- o dono da
        limpeza e' este arquivo. Esquecer de limpar custa o Esc do usuario no
        sistema inteiro, para sempre.
        """
        self._set_hotkey_recording(False)
        try:
            if self.hotkey is not None:
                self.hotkey.cancellable = False
        except Exception:
            self.log.exception("failed to clear hotkey.cancellable")

    def _arm_watchdog(self, sid: int) -> None:
        # Um max_record_sec torto no config.json nao pode explodir aqui: estamos
        # dentro do callback do hotkey, com a gravacao ja em andamento.
        try:
            secs = float(self.cfg.get("max_record_sec") or 0)
        except (TypeError, ValueError):
            secs = float(config.DEFAULTS["max_record_sec"])
        if secs <= 0:
            return

        def fire():
            with self._lock:
                if self._session != sid or self._state != _STATE_RECORDING:
                    return
                # Nada de mensagem agora: o spinner de "transcrevendo" repinta a
                # pilula no quadro seguinte e o usuario nunca chega a ler que o
                # corte foi automatico. O motivo viaja com o job e sai junto com a
                # confirmacao do ditado.
                self._auto_stop = (sid, secs)
            self.log.warning("max_record_sec (%.0fs) reached; auto-stopping session %d",
                             secs, sid)
            # Nos e' que estamos encerrando a gravacao: o engine so limpa as flags
            # dos eventos que ele proprio produziu, e `recording` presa em True
            # continua engolindo o Enter do usuario no sistema inteiro. on_stop()
            # desliga dentro do lock.
            self.on_stop()

        try:
            timer = threading.Timer(secs, fire)
            timer.daemon = True
            timer.name = "rec-watchdog"
            timer.start()
            self._watchdog = timer
        except Exception:
            # Sem watchdog o ditado ainda para no Enter; deixar a excecao subir
            # abortaria uma gravacao que ja esta acontecendo.
            self.log.exception("could not arm the max_record_sec watchdog")

    def _disarm_watchdog(self) -> None:
        timer, self._watchdog = self._watchdog, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _arm_stall_check(self, sid: int) -> None:
        """Rede de seguranca do estado 'transcribing'.

        Se a maquina ficar em transcricao sem job na fila E sem job em voo, nao
        existe ninguem para devolve-la a idle: o app aceita o proximo Win+A so
        para responder "ainda processando o ditado anterior", e a bandeja mente
        "Transcrevendo..." ate' o usuario matar o processo.
        """
        self._disarm_stall()

        def fire():
            with self._lock:
                if self._closing or self._session != sid:
                    return
                state = self._state
                active = self._active_job
            if state not in (_STATE_TRANSCRIBING, _STATE_INJECTING):
                return
            if active == sid or not self._jobs.empty():
                # Trabalho de verdade em andamento (transcricao longa na CPU, texto
                # grande sendo colado): so conferir de novo mais tarde.
                self._arm_stall_check(sid)
                return
            self.log.error("state machine stuck in %s for %.0fs with no job in flight "
                           "(session %d); forcing idle", state, STALL_CHECK_S, sid)
            self._fail(MSG_STALLED, sid=sid)

        try:
            timer = threading.Timer(STALL_CHECK_S, fire)
            timer.daemon = True
            timer.name = "stall-check"
            timer.start()
            self._stall = timer
        except Exception:
            self.log.exception("could not arm the transcribing stall check")

    def _disarm_stall(self) -> None:
        timer, self._stall = self._stall, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _disarm_timers(self) -> None:
        self._disarm_watchdog()
        self._disarm_stall()

    # ------------------------------------------------------------------
    # medidor de nivel da pilula
    # ------------------------------------------------------------------
    def _level_loop(self, sid: int) -> None:
        """Alimenta as barrinhas da pilula enquanto a gravacao acontece.

        NUNCA usar mark()/take() para isso. `take()` e a leitura da GRAVACAO: ela
        aplica o gate de silencio, emite os eventos 'silent_input'/'lapped' e
        reescreve o `last_audio` do Mic. Chamada 10x por segundo ela enche o log
        rotativo de 1 MB e apaga o diagnostico do ditado que estava acontecendo.
        A leitura barata e nao destrutiva e `level()`/`tail()`.
        """
        mic = self.mic
        if mic is None:
            return
        try:
            from wispr import audio as audio_mod
        except Exception:
            return

        read = None
        level = getattr(mic, "level", None)
        tail = getattr(mic, "tail", None)
        if callable(level):
            def read():
                return float(level(LEVEL_WINDOW_S))
        elif callable(tail):
            def read():
                return _rms(audio_mod, tail(LEVEL_WINDOW_S))
        if read is None:
            # Sem leitura nao destrutiva do ring as barras ficam paradas; o ditado
            # continua igual. Isso e preferivel a consumir a gravacao do usuario.
            self.log.debug("mic has no level()/tail(); overlay bars stay flat")
            return

        while True:
            with self._lock:
                if self._session != sid or self._state != _STATE_RECORDING or self._closing:
                    return
            if self._mic_feeds_level:
                return          # o proprio Mic esta mandando nivel por on_event
            try:
                self.overlay.set_level(read())
            except Exception:
                if not self._level_warned:
                    self._level_warned = True
                    self.log.exception("level feed failed; overlay bars go quiet")
                return
            time.sleep(LEVEL_INTERVAL)

    def _on_mic_event(self, *args, **kwargs) -> None:
        """Eventos do Mic. O vocabulario REAL do wispr.audio e' exatamente este:
        `open | device_changed | stream_dead | reopen_failed | silent_input |
        lapped` (mais um eventual push de nivel). O nome amigavel do endpoint
        chega como `device_name`: `name` e' o nome do EVENTO.

        O proprio wispr.audio ja registra cada evento no nivel certo e no mesmo
        arquivo de log; repetir a linha aqui so gastaria o rotativo de 1 MB. O que
        e' feito aqui e' o que so o app pode fazer: contar ao usuario, na pilula,
        que o microfone caiu no meio da fala dele.
        """
        kind = str(args[0]) if args else str(kwargs.get("event", "event"))
        rest = args[1:]
        try:
            if kind in ("level", "rms"):
                value = rest[0] if rest else kwargs.get("rms", kwargs.get("level"))
                if value is not None:
                    # Mic empurrando nivel: o nosso polling sai de cena.
                    self._mic_feeds_level = True
                    self.overlay.set_level(float(value))
                return
            if kind in ("stream_dead", "reopen_failed"):
                self._note_mic_fault(kind)
                return
            if kind in ("device_changed", "open"):
                self.log.info("mic %s: %s", kind, kwargs or rest)
                return
            # silent_input e lapped ja viram aviso no lugar certo: o primeiro vira
            # MSG_NO_SIGNAL no fim da gravacao, o segundo viaja no status do take().
            self.log.debug("mic event: %s %s %s", kind, rest, kwargs)
        except Exception:
            self.log.debug("mic event handling failed", exc_info=True)

    def _note_mic_fault(self, kind: str) -> None:
        """Stream caido durante a gravacao: avisar AGORA, nao no fim.

        Fora de uma gravacao isto e' rotina do supervisor (uma abertura WASAPI
        exclusiva alheia derruba o nosso stream e ele reabre em 0,75 s), e nao
        vale interromper o usuario por isso.
        """
        with self._lock:
            if self._state != _STATE_RECORDING:
                return
            sid = self._session
            last = self._mic_fault[1]
            now = time.monotonic()
            self._mic_fault = (sid, now)
        if now - last < MIC_FAULT_MSG_S:
            return       # reopen_failed repete em backoff; a pilula nao pisca junto
        self.log.warning("microphone %s during session %d", kind, sid)
        self._message(MSG_MIC_LOST, 2600)

    # ------------------------------------------------------------------
    # worker: gate de silencio -> resample -> ASR -> sanitize -> injecao
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            job = self._jobs.get()
            # No shutdown o motor e o microfone ja foram fechados: processar um
            # ditado agora so produziria erro em cima de erro.
            if job is None or self._closing:
                return
            try:
                self._process(*job)
            except Exception:
                self.log.exception("worker crashed on a dictation")
                try:
                    self._fail(MSG_STT_ERROR, sid=job[0])
                except Exception:
                    # Esta thread nao pode morrer: sem ela todo ditado seguinte
                    # ficaria pendurado em 'transcrevendo' para sempre.
                    self.log.exception("failure handling crashed too")

    def _process(self, sid, audio_48k, status, stamp, rec_s, flags=None) -> None:
        from wispr import audio as audio_mod
        from wispr import stt as stt_mod

        flags = flags or {}
        auto_s = float(flags.get("auto_stop") or 0.0)
        truncated = bool(flags.get("truncated"))
        timings = {"record_s": rec_s, "resample_ms": 0.0, "transcribe_ms": 0.0,
                   "inject_ms": 0.0}
        x16 = None
        text = ""
        mode = "-"
        sent = 0
        degraded = False    # quem falhou ja pintou o icone; o finally nao apaga

        def fail(msg):
            nonlocal degraded
            degraded = True
            if auto_s > 0:
                # O corte automatico e' fato do ditado inteiro, inclusive quando
                # ele termina em erro: a mensagem de falha e' a unica que o
                # usuario le, entao o motivo viaja nela.
                msg = (MSG_MAXREC % int(auto_s)) + " " + msg
            self._fail(msg, sid=sid)

        with self._lock:
            self._active_job = sid
        try:
            # Silencio digital: o headset entrega amostras exatamente 0.0 com o
            # Windows jurando que esta tudo ativo. So o RMS pega. Secao 4.
            if not audio_mod.Mic.has_signal(audio_48k):
                self.log.warning("no signal in %.2fs of audio (digital silence)", rec_s)
                fail(MSG_MIC_LOST if truncated else MSG_NO_SIGNAL)
                return

            t0 = time.perf_counter()
            x16 = audio_mod.to_whisper(audio_48k)
            timings["resample_ms"] = (time.perf_counter() - t0) * 1000.0

            engine = self._get_engine()
            if engine is None:
                loading = self._engine_state == "loading"
                fail(MSG_MODEL_LOADING if loading else MSG_STT_ERROR)
                return

            t0 = time.perf_counter()
            try:
                raw = engine.transcribe(x16)
            except Exception as exc:
                self.log.exception("transcription failed on backend=%s",
                                   getattr(engine, "backend", "?"))
                retry = self._fallback_to_cpu(engine)
                if retry is None:
                    fail(self._stt_message(stt_mod, exc))
                    return
                try:
                    raw = retry.transcribe(x16)
                except Exception as exc2:
                    self.log.exception("transcription failed again after the cpu fallback")
                    fail(self._stt_message(stt_mod, exc2))
                    return
            timings["transcribe_ms"] = (time.perf_counter() - t0) * 1000.0

            text = self._sanitize(raw)
            if not text:
                self.log.info("empty transcript (%.2fs of audio)", rec_s)
                fail(MSG_MIC_LOST if truncated else MSG_EMPTY)
                return

            if self._cancelled == sid:
                self.log.info("session %d cancelled before injection; dropping %d chars",
                              sid, len(text))
                # A pilula so e' nossa se nenhuma sessao nova comecou. O Esc deixa
                # a transcricao terminar de propria vontade (ver on_cancel) para o
                # usuario poder ditar de novo na hora; se ele ja fez isso, o
                # `on_start` incrementou `_session`, chamou `show_recording()` e
                # esta pilula e' a da gravacao DE AGORA. Esconde-la deixaria o
                # `set_level` alimentando uma barrinha invisivel e o usuario
                # falando sem nenhum retorno na tela ate' o proximo Enter.
                with self._lock:
                    mine = self._session == sid
                if mine:
                    self.overlay.hide()
                return

            with self._lock:
                if self._session != sid:
                    self.log.info("session %d superseded; dropping transcript", sid)
                    return
                self._state = _STATE_INJECTING

            # Guardar ANTES de entregar: se a injecao falhar, o texto continua no
            # historico e no "Colar ultima transcricao" da bandeja.
            self._remember(text)

            # O aviso vem ANTES da entrega. Depois dela a meia-frase ja esta dentro
            # do e-mail do usuario, e a pilula so confirmaria o que ele nao pode
            # mais desfazer -- e se a entrega cair para o clipboard, a mensagem dela
            # sobrescreve esta, que e' o certo: clipboard e' mais urgente.
            notice = ""
            if auto_s > 0:
                notice = MSG_MAXREC % int(auto_s)
            if status == "lapped":
                notice = (notice + " " + MSG_LAPPED).strip()
            if notice:
                self._message(notice, 3000)

            t0 = time.perf_counter()
            mode, sent = self._deliver(text)
            timings["inject_ms"] = (time.perf_counter() - t0) * 1000.0

            if mode in _MODES_DEGRADED:
                # 'clipboard' (UIPI ou falha total) e 'partial' (so um pedaco
                # entrou): os dois ja pintaram o icone de erro e deixaram um aviso
                # na pilula. Esconde-la aqui apagaria a unica coisa que o usuario
                # tem para ler.
                degraded = True
            elif mode in _MODES_WITH_MESSAGE:
                pass                # 'empty': nao e' erro, mas a pilula tem recado
            elif not notice:
                self.overlay.hide()
            # `sent` e' o que a injecao confirmou ter entregue; o denominador e' so
            # o que a gente tinha para entregar. Logar o segundo como se fosse o
            # primeiro e' o que fazia o log jurar "chars=57" com o texto em lugar
            # nenhum sob UIPI. Os dois em code units UTF-16, a unidade do
            # `Delivery.chars`: com `len(text)` um emoji fazia o numerador passar
            # do denominador.
            self.log.info(
                "dictation ok: record=%.2fs resample=%.1fms transcribe=%.1fms "
                "inject=%.1fms chars=%d/%d (utf-16) mode=%s",
                timings["record_s"], timings["resample_ms"], timings["transcribe_ms"],
                timings["inject_ms"], sent, _utf16_units(text), mode)
        finally:
            if self.cfg.get("keep_recordings") and x16 is not None:
                self._save_recording(x16, stamp)
            with self._lock:
                self._active_job = None
                current = self._session == sid
                if current and self._state in (_STATE_TRANSCRIBING, _STATE_INJECTING):
                    self._state = _STATE_IDLE
            if current:
                # Fim do ditado: o Esc volta a ser do aplicativo em foco. Se outra
                # sessao ja comecou, as flags sao DELA -- limpar aqui engoliria o
                # cancelamento do ditado novo.
                self._disarm_stall()
                self._clear_hotkey_flags()
            # Se o usuario ja comecou outro ditado, o icone dele e que vale: pintar
            # 'idle' aqui apagaria o vermelho de "gravando" da sessao nova.
            if current and not degraded:
                self._tray_state(_STATE_IDLE)

    def _stt_message(self, stt_mod, exc: BaseException) -> str:
        """Mensagem para o usuario a partir da excecao de transcricao.

        `stt.TranscriptionError` existe justamente para carregar texto em pt-BR
        pronto para a tela ("Groq sem chave: preencha groq_api_key..."). Trocar
        todas elas pela mesma constante fazia uma chave de API faltando parecer
        identica a um crash de GPU.
        """
        try:
            if isinstance(exc, stt_mod.TranscriptionError):
                msg = _one_line(str(exc))
                if msg:
                    return msg
        except Exception:
            pass
        return MSG_STT_ERROR

    def _sanitize(self, raw) -> str:
        """Aparo o texto e aplico os fixups. postprocess e idempotente para essas
        substituicoes, entao nao tem problema se a Engine ja tiver rodado."""
        text = str(raw or "").strip()   # str(): a Engine pode devolver qualquer coisa
        if not text:
            return ""
        try:
            from wispr import stt
            text = stt.postprocess(text, self.cfg.get("fixups") or ())
        except Exception:
            self.log.exception("postprocess failed; using raw transcript")
        text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
        return text.strip()

    def _deliver(self, text: str):
        """Entrega o texto na janela em foco. Devolve (modo, chars entregues).

        O modo e' 'type', 'paste', 'clipboard', 'partial' ou 'empty'. Os
        diagnosticos sao lidos por ATRIBUTO, com getattr: `inject.deliver()`
        devolve um `inject.Delivery`, que e' subclasse de `str` e so imita
        mapeamento por keys()/get(). Um `isinstance(res, dict)` da False nele, e
        era isso que matava, de uma vez, as redes de seguranca abaixo --
        inclusive a unica evidencia que o app tem de que o texto chegou mesmo, ja
        que sob UIPI tanto o SendInput quanto o Ctrl+V somem com
        GetLastError() == 0 (ARCHITECTURE.md secao 5).

        A ORDEM das checagens e' o que torna cada uma correta:

        1. `blocked` primeiro, porque sob UIPI o `chars` mente: o SendInput
           devolve sucesso e o texto nao aparece em lugar nenhum.
        2. `empty` antes de `chars`, porque "nao havia texto" nao e' falha.
        3. `chars == 0` e' o UNICO caso em que o clipboard e' seguro.
        4. `0 < chars < esperado` e' entrega PARCIAL, e ai o clipboard e' proibido:
           colar o texto inteiro por cima duplicaria o pedaco que ja entrou.
        """
        from wispr import inject

        if foreground_is_elevated():
            self.log.warning("foreground window is elevated; injection blocked by UIPI")
            return self._clipboard_fallback(inject, text, MSG_ELEVATED_CLIP, MSG_ELEVATED)

        res = inject.deliver(text, self.cfg)
        mode = str(getattr(res, "mode", None) or res or "type")
        chars = getattr(res, "chars", None)
        elevated = getattr(res, "target_elevated", None)
        blocked = getattr(res, "blocked", None)
        lost = bool(getattr(res, "lost_nontext_formats", False))
        if chars is not None:
            # Uma unica conversao, aqui: `chars` vem de outro modulo e um valor
            # esquisito nao pode explodir no meio das checagens. None = "nao sei".
            try:
                chars = int(chars)
            except (TypeError, ValueError):
                self.log.warning("inject reported chars=%r, which is not a number", chars)
                chars = None

        # `blocked` = "o UIPI vai engolir isto?" (alvo elevado E nos nao) e' a
        # resposta ACIONAVEL; `target_elevated` e' so' "a janela e' de admin?".
        # Agir na crua era alarme falso caro: rodar elevado e' justamente o que a
        # ARCHITECTURE.md secao 3 recomenda para o hook sobreviver a janelas
        # elevadas, e ai TODA janela de admin virava "falhou" -- o texto entrava
        # na janela, ia tambem para o clipboard, e o Ctrl+V do usuario escrevia
        # tudo pela segunda vez. So caimos na crua quando a acionavel nao existe
        # (inject antigo) ou nao soube responder.
        if blocked is None:
            blocked = elevated
        elif elevated and not blocked:
            self.log.debug("elevated target, but wisper is elevated too: injection goes through")
        if blocked:
            self.log.warning("inject reported the target as blocked (elevated=%r) after our "
                             "own check passed; using the clipboard", elevated)
            return self._clipboard_fallback(inject, text, MSG_ELEVATED_CLIP, MSG_ELEVATED)

        # O que o inject deveria ter entregue, medido na MESMA unidade do `chars`
        # (code units UTF-16) e depois do MESMO saneamento: o `inject.sanitize()`
        # derruba U+200B, BOM e surrogate solto, entao comparar com o texto cru
        # faria um saneamento normal parecer entrega parcial.
        expected = self._expected_units(inject, text)

        if bool(getattr(res, "empty", False)) or mode == _MODE_EMPTY or (
                expected <= 0 and (chars is None or chars <= 0)):
            # Nao havia o que entregar (a transcricao era so' um U+200B, por
            # exemplo). Nao e' falha: nada de icone vermelho, e sobretudo nada de
            # mandar string vazia para a area de transferencia do usuario, que e'
            # onde talvez esteja algo que ele guardou.
            self.log.info("nothing to inject: the text was empty after inject.sanitize()")
            self._message(MSG_NOTHING, 1800)
            return _MODE_EMPTY, 0

        if chars is not None:
            if chars <= 0:
                # SendInput/Ctrl+V falharam: o inject loga, degrada e devolve chars=0.
                # Sem este ramo o app anunciaria sucesso de um texto que nunca apareceu.
                self.log.error("injection delivered 0 chars (mode=%s); using the clipboard",
                               mode)
                return self._clipboard_fallback(inject, text, MSG_INJECT_FAIL, MSG_STT_ERROR)
            if chars < expected:
                # Entrega PARCIAL. O inject ja logou "partial delivery" e parou de
                # proposito. Aqui o clipboard esta PROIBIDO: o comeco do texto ja
                # esta dentro do e-mail do usuario e um Ctrl+V colaria o texto
                # inteiro em cima dele. O texto completo continua no historico
                # (`_remember` roda antes da entrega) e na acao da bandeja.
                self.log.error("partial injection: %d of %d utf-16 code units (mode=%s); "
                               "not falling back to the clipboard, a paste would duplicate "
                               "what already landed", chars, expected, mode)
                self._message(MSG_INJECT_PARTIAL, 5000)
                self._notify("wisper", MSG_INJECT_PARTIAL)
                self._tray_error_icon()
                return _MODE_PARTIAL, chars

        if lost:
            # Um CF_DIB no clipboard nao sobrevive a um restore so de CF_UNICODETEXT.
            self.log.warning("clipboard had non-text formats; they were lost on restore")
            self._message(MSG_CLIP_IMAGE, 2600)
        # chars None so acontece com um inject antigo, que nao reporta nada: nesse
        # caso o que foi entregue e' o que foi pedido, e e' o melhor que se sabe.
        return mode, chars if chars is not None else len(text)

    def _expected_units(self, inject, text: str) -> int:
        """Quantas code units UTF-16 a entrega deste texto deveria somar.

        Passa pelo mesmo `inject.sanitize()` do `deliver()` porque so assim os
        dois numeros falam da mesma coisa. O espaco final do `trailing_space` fica
        de fora de proposito: ele so pode fazer o `chars` ficar MAIOR que este
        numero, e um `chars` maior nunca e' lido como entrega parcial.
        """
        s = text
        try:
            s = inject.sanitize(text)
        except Exception:
            self.log.debug("inject.sanitize failed while sizing the text", exc_info=True)
            s = text
        return _utf16_units(s)

    def _clipboard_fallback(self, inject, text: str, ok_msg: str, fail_msg: str):
        """Ultimo recurso: deixa o texto na area de transferencia e avisa.

        Medido: dentro de janela elevada o SendInput e o Ctrl+V sao descartados em
        SILENCIO, com GetLastError() == 0 -- nao ha erro para tratar depois. O
        Ctrl+V do proprio usuario, esse, funciona. Ver ARCHITECTURE.md secao 5.
        """
        saved = False
        try:
            inject.clip_set_text(text)
            saved = True
        except Exception:
            self.log.exception("clipboard fallback failed")
        msg = ok_msg if saved else fail_msg
        self._message(msg, 4000)
        self._notify("wisper", msg)
        self._tray_error_icon()
        # Zero caracteres ENTREGUES: o texto esta na area de transferencia e quem
        # cola e' o usuario. O log tem que dizer isso, nao "chars=57, tudo certo".
        return _MODE_CLIPBOARD, 0

    def _remember(self, text: str) -> None:
        try:
            limit = max(1, int(self.cfg.get("history_size") or 1))
        except (TypeError, ValueError):
            limit = int(config.DEFAULTS["history_size"])
        with self._lock:
            self.last_text = text
            self.history.append(text)
            if len(self.history) > limit:
                del self.history[:-limit]

    def _save_recording(self, x16, stamp: datetime) -> None:
        """Wav 16 kHz em logs/recordings, nome ordenavel pelo instante da fala."""
        try:
            from wispr import audio as audio_mod
            sr = int(getattr(audio_mod, "TARGET_SR", 0) or self.cfg.get("target_sr", 16000))
            folder = config.LOG_DIR / "recordings"
            folder.mkdir(parents=True, exist_ok=True)
            name = stamp.strftime("%Y%m%d-%H%M%S-") + f"{stamp.microsecond // 1000:03d}.wav"
            path = folder / name
            pcm = np.clip(np.asarray(x16, dtype=np.float32), -1.0, 1.0)
            pcm = (pcm * 32767.0).astype(np.int16)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sr)
                wav.writeframes(pcm.tobytes())
            self.log.info("recording saved: %s", path)
        except Exception:
            self.log.exception("failed to save recording")

    # ------------------------------------------------------------------
    # feedback ao usuario
    # ------------------------------------------------------------------
    def _message(self, text: str, ms: int = 1800) -> None:
        try:
            self.overlay.show_message(text, ms)
        except Exception:
            self.log.exception("overlay.show_message failed")

    def _notify(self, title: str, msg: str) -> None:
        if not self.cfg.get("notify_on_error", True):
            return
        try:
            self.tray.notify(title, msg)
        except Exception:
            self.log.exception("tray.notify failed")

    def _tray_state(self, state: str) -> None:
        try:
            self.tray.set_state(state)
        except Exception:
            self.log.exception("tray.set_state(%s) failed", state)

    def _tray_error_icon(self) -> None:
        self._tray_state("error")
        timer, self._icon_timer = self._icon_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

        def back():
            with self._lock:
                if self._state != _STATE_IDLE or self._closing:
                    return
            self._tray_state(_STATE_IDLE)

        # Falhar ao subir esta thread nao pode escapar: _tray_error_icon roda de
        # dentro de _fail, ou seja, ja dentro de um tratamento de erro.
        try:
            timer = threading.Timer(ERROR_ICON_S, back)
            timer.daemon = True
            timer.start()
            self._icon_timer = timer
        except Exception:
            self.log.exception("could not arm the tray icon reset timer")

    def _fail(self, msg: str, notify: bool = True, sid: int | None = None) -> None:
        """Toda falha termina aqui: log ja foi feito, aqui vai o que o usuario ve.

        `sid` e obrigatorio para qualquer falha que venha da thread worker: sem
        ele, um ditado que falhou tarde zeraria o estado (e as flags do hotkey) de
        uma gravacao NOVA que o usuario ja tinha comecado.
        """
        with self._lock:
            stale = sid is not None and self._session != sid
            if not stale:
                self._state = _STATE_IDLE
                self._clear_hotkey_flags()
        if not stale:
            self._disarm_timers()
        if stale:
            self.log.warning("failure from superseded session %d not shown: %s", sid, msg)
            return
        self.log.warning("failure shown to user: %s", msg)
        self._message(msg, 2600)
        self.ping_error()
        self._tray_error_icon()
        if notify:
            self._notify("wisper", msg)

    def _sound(self, kind: str) -> None:
        if not self.cfg.get("sounds", True):
            return
        wav = config.ASSETS_DIR / ("%s.wav" % kind)
        try:
            has_wav = wav.is_file()
        except OSError:
            has_wav = False
        if not has_wav and kind in ("start", "stop"):
            # O overlay ja tem os bipes prontos em memoria, com fila propria: nada
            # de uma thread por ping nem de abrir o endpoint de saida no caminho
            # quente do ditado.
            try:
                from wispr import overlay as overlay_mod
                fn = getattr(overlay_mod, "ping_" + kind, None)
                if callable(fn):
                    fn(self.cfg)
                    return
            except Exception:
                self.log.debug("overlay ping unavailable; falling back to winsound",
                               exc_info=True)

        def play():
            try:
                import winsound
                if has_wav:
                    winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC
                                       | winsound.SND_NODEFAULT)
                elif kind == "error":
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                else:
                    winsound.Beep(880 if kind == "start" else 620, 70)
            except Exception:
                pass

        # winsound.Beep bloqueia; numa thread daemon ele nao atrasa o ditado.
        # O `start()` precisa estar protegido: `ping_stop()` roda no caminho quente
        # do on_stop, e um RuntimeError de "can't start new thread" ali escapava
        # para o hook engine e deixava a maquina de estados presa em transcricao.
        try:
            threading.Thread(target=play, name="ping", daemon=True).start()
        except Exception:
            self.log.debug("ping thread did not start", exc_info=True)

    def ping_start(self) -> None:
        self._sound("start")

    def ping_stop(self) -> None:
        self._sound("stop")

    def ping_error(self) -> None:
        self._sound("error")

    # ------------------------------------------------------------------
    # acoes expostas a bandeja
    # ------------------------------------------------------------------
    def toggle_enabled(self) -> None:
        with self._lock:
            self.enabled = not self.enabled
            recording = self._state == _STATE_RECORDING
        self.log.info("dictation %s by user", "enabled" if self.enabled else "disabled")
        if not self.enabled and recording:
            self.on_cancel()
        self._message(MSG_ENABLED_ON if self.enabled else MSG_ENABLED_OFF, 1400)
        # Pausado nao e erro: o icone vermelho de X e para falha. A bandeja le
        # app.enabled sozinha e mostra "pausado" no tooltip e no menu.
        self._tray_state(_STATE_IDLE)

    def repaste_last(self) -> None:
        # A maquina de estados entra em 'injecting' JA AQUI, no mesmo lock que le
        # `last_text`. Sem isso dois cliques seguidos no menu passavam os dois pela
        # checagem de ocupado e dois `_deliver()` corriam juntos -- duas colagens
        # embaralhadas na mesma janela, e o mesmo buraco deixava um repaste
        # atropelar a injecao de um ditado de verdade.
        with self._lock:
            text = self.last_text
            busy = self._state != _STATE_IDLE
            if not busy and text:
                self._state = _STATE_INJECTING
        if busy:
            self._message(MSG_BUSY)
            return
        if not text:
            self._message(MSG_NO_HISTORY)
            return

        def go():
            try:
                t0 = time.perf_counter()
                mode, sent = self._deliver(text)
                self.log.info("repaste: chars=%d/%d (utf-16) mode=%s inject=%.1fms",
                              sent, _utf16_units(text), mode,
                              (time.perf_counter() - t0) * 1000.0)
                if mode not in _MODES_WITH_MESSAGE:
                    # Os modos com recado (clipboard, partial, empty) ja escreveram
                    # na pilula; o hide zeraria o `msg_until` e apagaria o aviso.
                    self.overlay.hide()
            except Exception:
                self.log.exception("repaste failed")
                self._message("Falha ao reinserir o texto (ver logs).", 2400)
            finally:
                with self._lock:
                    # So devolve o que esta reservado para o repaste: qualquer outro
                    # estado aqui e' de um ditado novo e nao e' nosso para mexer.
                    if self._state == _STATE_INJECTING:
                        self._state = _STATE_IDLE
                        # E as flags do hotkey junto. O repaste tambem estaciona a
                        # maquina em 'injecting', e um Win+A que chegue nesse meio
                        # tempo e' recusado la em cima SEM limpar nada (ali existe
                        # um ditado vivo que ainda precisa do Enter e do Esc). Se
                        # ninguem limpasse aqui, `cancellable` ficava presa em True
                        # sem ditado nenhum e o hook engolia o Esc do Windows
                        # INTEIRO -- so' se curando ao comer um Esc do usuario.
                        self._clear_hotkey_flags()

        try:
            # nunca na thread da bandeja: injetar pode levar segundos em texto longo.
            threading.Thread(target=go, name="repaste", daemon=True).start()
        except Exception:
            with self._lock:
                if self._state == _STATE_INJECTING:
                    self._state = _STATE_IDLE
                    self._clear_hotkey_flags()   # mesma regra do finally do go()
            self.log.exception("could not start the repaste thread")
            self._message("Falha ao reinserir o texto (ver logs).", 2400)

    def open_config(self) -> None:
        try:
            if not config.CONFIG_PATH.exists():
                config.save(self.cfg)
            os.startfile(str(config.CONFIG_PATH))   # noqa: S606 - acao pedida pelo usuario
            self.log.info("opened %s", config.CONFIG_PATH)
        except Exception:
            self.log.exception("failed to open config")
            self._message("Nao consegui abrir o config.json (ver logs).", 2400)

    def open_logs(self) -> None:
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(config.LOG_DIR))       # noqa: S606 - acao pedida pelo usuario
            self.log.info("opened %s", config.LOG_DIR)
        except Exception:
            self.log.exception("failed to open logs folder")
            self._message("Nao consegui abrir a pasta de logs (ver logs).", 2400)

    def quit(self) -> None:
        """Alias para a bandeja: 'Sair'."""
        self.shutdown()

    # ------------------------------------------------------------------
    # desligamento
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Idempotente e chamavel de qualquer thread, inclusive pelo atexit.

        Quem chamou primeiro faz o trabalho; os outros voltam na hora. Quem
        precisa esperar o fim de verdade e' o `run()`, pelo `_shutdown_over`.
        """
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._closing = True
        try:
            self._shutdown_steps()
        finally:
            # Aconteca o que acontecer, o `run()` nao pode ficar esperando 15 s
            # por um shutdown que ja terminou (bem ou mal).
            self._shutdown_over.set()

    def _shutdown_steps(self) -> None:
        log = self.log
        log.info("shutdown started")

        self._disarm_timers()
        for timer in (self._icon_timer,):
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

        # Antes de desinstalar o hook: se o `stop()` dele falhar, o hook fica de pe
        # mais um pouco, e com as flags presas ele engoliria Enter e Esc de um app
        # que nem existe mais.
        self._clear_hotkey_flags()

        try:
            self._jobs.put_nowait(None)   # solta o worker do get() bloqueante
        except Exception:
            pass

        # Ordem: primeiro o que produz eventos novos (hotkey), depois a UI, e so
        # entao os donos de recurso. `engine.close()` espera o lock do modelo, ou
        # seja, pode segurar aqui o tempo de uma transcricao em andamento; a
        # bandeja ja tem que ter saido antes disso para o app parecer que fechou.
        # O preload entra no meio de proposito: ele pode estar criando AGORA o
        # motor que o passo "engine" logo abaixo tem que fechar.
        for what, fn in (
            ("hotkey", lambda: self.hotkey.stop() if self.hotkey else None),
            ("tray", lambda: self.tray.stop()),
            ("preload", self._join_preload),
            ("mic", lambda: self.mic.close() if self.mic else None),
            ("engine", lambda: self.engine.close() if self.engine else None),
            ("overlay", lambda: self.overlay.stop()),
        ):
            try:
                fn()
            except Exception:
                log.exception("error stopping %s", what)

        handle, self._mutex = self._mutex, None
        if handle:
            try:
                # So CloseHandle: o mutex e' criado sem posse (bInitialOwner=False),
                # entao nao ha nada a liberar -- e um ReleaseMutex daqui falharia
                # com ERROR_NOT_OWNER sempre que o shutdown viesse da bandeja, que
                # e' outra thread. O handle fechado ja apaga o nome para a proxima
                # instancia.
                _k32.CloseHandle(handle)
            except Exception:
                log.exception("failed to close the single-instance mutex")

        log.info("shutdown complete")
        try:
            for logger in (log, self._root_log, logging.getLogger()):
                if logger is None:
                    continue
                for handler in list(logger.handlers):
                    try:
                        handler.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    def _join_preload(self) -> None:
        """Espera a thread de preload antes de fechar o motor.

        Sair nos primeiros segundos deixava o modelo terminando de carregar
        DEPOIS do "shutdown complete": ~1 GB de VRAM e um contexto CUDA sem dono,
        numa thread daemon que a finalizacao do interpretador mata dentro do
        ctranslate2. Se o tempo acabar, o proprio `_build_engine` ainda confere
        `_closing` antes de atribuir e fecha o que criou.
        """
        thread, self._preload_thread = self._preload_thread, None
        if thread is None or not thread.is_alive():
            return
        self.log.info("waiting up to %.0fs for the model preload to finish", PRELOAD_JOIN_S)
        thread.join(PRELOAD_JOIN_S)
        if thread.is_alive():
            self.log.warning("preload still loading at shutdown; it closes its own engine")


def _one_line(text, limit: int = MSG_MAX_CHARS) -> str:
    """Mensagem de uma linha, do tamanho que a pilula aceita.

    Texto de excecao vem de fora (stt.TranscriptionError) e pode ter quebra de
    linha: a pilula mede uma linha so para dimensionar a janela, e o resto
    vazaria para fora dela.
    """
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[:limit - 1].rstrip() + "..."
    return s


def _utf16_units(text) -> int:
    """Quantas code units UTF-16 o texto tem.

    E' a unidade do `inject.Delivery.chars` -- o que o `SendInput` manda de fato
    --, e nao o `len()` do Python: um emoji e' um code point so aqui e DUAS code
    units la. Medir com `len()` faria cada emoji parecer um caractere faltando.
    """
    try:
        return len(str(text).encode("utf-16-le")) // 2
    except Exception:
        # Surrogate solto nao codifica em UTF-16 estrito. O inject derruba esses
        # no sanitize(); aqui basta nao explodir.
        return len(str(text))


def _rms(audio_mod, x) -> float:
    """RMS do bloco. Usa audio.rms() se existir; senao calcula aqui mesmo."""
    fn = getattr(audio_mod, "rms", None)
    if callable(fn):
        try:
            return float(fn(x))
        except Exception:
            pass
    try:
        # float64 no acumulador: em float32 a soma satura em blocos longos, e um
        # NaN de driver quebrado deixaria a barrinha maluca.
        arr = np.asarray(x, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return 0.0
        value = float(np.sqrt(np.dot(arr, arr) / arr.size))
    except Exception:
        return 0.0
    return value if np.isfinite(value) else 0.0


def main() -> int:
    """Ponto de entrada tambem util para `python -m wispr.app`."""
    try:
        return App().run()
    except Exception:
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (config.LOG_DIR / "crash.log").open("a", encoding="utf-8") as fh:
                fh.write("\n=== %s ===\n%s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                               traceback.format_exc()))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
