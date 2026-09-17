# -*- coding: utf-8 -*-
"""Ícone de bandeja do wisper (pystray + Pillow).

A bandeja é a ÚNICA interface visível do app: ele roda sob `pythonw.exe`, sem
console. Se o ícone morrer, o processo continua vivo, invisível e sem nenhuma
forma de ser encerrado pela UI — por isso cada callback de menu, cada
propriedade dinâmica e cada balão de notificação aqui é blindado com
try/except. Nada neste módulo pode propagar exceção para dentro do pystray.

Threading (ver docs/ARCHITECTURE.md seção 6):
    `Tray.run()` BLOQUEIA e tem que ser chamado na THREAD PRINCIPAL. O backend
    win32 do pystray cria a janela oculta do ícone e roda o próprio
    bombeamento de mensagens Win32 nessa thread. Foi medido que isso coexiste
    sem problema com o hook WH_KEYBOARD_LL rodando na thread dele.
    `set_state()` e `notify()` são chamados de outras threads e só mexem no que
    o pystray aceita de fora: ícone, título e balão.
    Nenhum callback de menu pode BLOQUEAR: enquanto ele roda, a thread
    principal não bombeia mensagens e o Windows marca a janela como travada.
    Por isso clipboard, troca de motor e o próprio `App.shutdown()` saem para
    threads daemon — inclusive o "Sair", que sem isso segurava o pump por até
    ~13 s de joins e locks com o ícone já removido da bandeja.

Regra de lock: este módulo NUNCA chama o app segurando `self._lock`. O app
chama `set_state()`/`notify()` de dentro das threads dele, e inverter essa
ordem criaria um abraço mortal entre o lock do App e o lock da bandeja.
"""
from __future__ import annotations

import ctypes
import functools
import os
import threading
from pathlib import Path

from wispr import config
from wispr import logging_setup

# pystray e Pillow são as únicas dependências de terceiros da bandeja. Se
# faltarem, o app inteiro ainda deve ditar texto — só fica sem ícone.
try:
    import pystray
    from PIL import Image, ImageDraw
    _UI_IMPORT_ERROR = None
except Exception as _exc:  # ImportError, mas também DLL quebrada do Pillow
    pystray = None
    Image = ImageDraw = None
    _UI_IMPORT_ERROR = _exc

log = logging_setup.get(__name__)

APP_NAME = "wisper"

STATES = ("idle", "recording", "working", "error")

# A máquina de estados do app (docs/CONTRACT.md) fala 'transcribing' e
# 'injecting', que não existem no contrato da bandeja. Mapear em vez de ignorar.
_STATE_ALIASES = {
    "idle": "idle", "ready": "idle", "pronto": "idle", "paused": "idle",
    "recording": "recording", "rec": "recording", "gravando": "recording",
    "working": "working", "transcribing": "working", "injecting": "working",
    "busy": "working", "loading": "working",
    "error": "error", "failed": "error", "erro": "error",
}

_STATE_LABELS = {
    "idle": "pronto",
    "recording": "gravando",
    "working": "transcrevendo",
    "error": "erro (veja os logs)",
}

# (cor de fundo, cor do glifo) — cores propositalmente muito distantes entre si,
# porque a bandeja renderiza isso a 16x16 e o usuário julga pela cor, não pela forma.
_PALETTE = {
    "idle": ((108, 140, 116, 255), (233, 242, 234, 255)),      # verde-acinzentado
    "recording": ((214, 58, 52, 255), (255, 244, 242, 255)),   # vermelho
    "working": ((233, 166, 46, 255), (38, 28, 8, 255)),        # âmbar
    "error": ((150, 22, 22, 255), (255, 255, 255, 255)),       # vermelho escuro + X
}

_ICON_FILES = {s: "tray_%s.png" % s for s in STATES}

HISTORY_SLOTS = 8          # o menu mostra só os últimos poucos; cfg.history_size guarda o resto
_ITEM_TEXT_MAX = 52
_STATUS_MAX = 48
# szTip do NOTIFYICONDATA tem 128 wchar, szInfoTitle 64 e szInfo 256. O pystray
# copia direto para esses campos: passar do tamanho levanta ValueError dentro do
# ctypes dele. Truncar aqui, antes, com folga.
_TIP_MAX = 120
_NOTIFY_TITLE_MAX = 60
_NOTIFY_MSG_MAX = 240

_ENGINE_LABELS = {"local": "Local (GPU)", "groq": "Groq (nuvem)"}


# --------------------------------------------------------------------------- #
# helpers de texto
# --------------------------------------------------------------------------- #
def _amp(text) -> str:
    """'&' vira sublinhado de acelerador no menu do Windows; '&&' imprime um '&'."""
    return str(text).replace("&", "&&")


def _one_line(text, limit: int = 0) -> str:
    """Colapsa espaços/quebras (transcrição tem \\n) e trunca com reticências.

    Só para RÓTULO. O texto original nunca passa por aqui antes de ir para o
    clipboard: colapsar as quebras de linha destruiria a transcrição.
    """
    s = " ".join(str(text).split())
    if limit and len(s) > limit:
        s = s[: max(1, limit - 1)].rstrip() + "…"
    return s


def _as_int(value, fallback: int = 0) -> int:
    """Contador vindo de outro módulo: pode ser None, str ou nem existir."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


_KEY_NAMES = {
    "win": "Win", "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "shift": "Shift", "esc": "Esc", "escape": "Esc", "enter": "Enter",
    "space": "Espaço", "tab": "Tab",
}


def _pretty_hotkey(spec) -> str:
    parts = [p.strip() for p in str(spec or "").split("+") if p.strip()]
    out = []
    for p in parts:
        low = p.lower()
        out.append(_KEY_NAMES.get(low, p.upper() if len(p) == 1 else p.capitalize()))
    return "+".join(out) or "?"


_ELEVATED = []      # cache de 1 posição: elevação não muda durante a vida do processo


def _is_elevated():
    """True/False, ou None se nem der para perguntar.

    WinDLL local em vez de `ctypes.windll.shell32`: o `windll` é um cache global
    do processo e setar restype nele vazaria para os outros módulos, que também
    falam com a API do Windows por ctypes.
    """
    if _ELEVATED:
        return _ELEVATED[0]
    value = None
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        fn = shell32.IsUserAnAdmin
        fn.argtypes = []
        fn.restype = ctypes.c_int          # BOOL
        value = bool(fn())
    except Exception:
        log.debug("IsUserAnAdmin failed", exc_info=True)
        value = None
    _ELEVATED.append(value)
    return value


def _normalize_state(state):
    return _STATE_ALIASES.get(str(state or "").strip().lower())


# --------------------------------------------------------------------------- #
# desenho dos ícones
# --------------------------------------------------------------------------- #
def _draw_mic(d, fg) -> None:
    """Cápsula + arco + haste: formas primitivas só, sem rounded_rectangle, que
    só existe no Pillow >= 8.2."""
    d.ellipse((26, 15, 38, 27), fill=fg)
    d.rectangle((26, 21, 38, 35), fill=fg)
    d.ellipse((26, 29, 38, 41), fill=fg)
    d.arc((20, 26, 44, 50), start=0, end=180, fill=fg, width=4)
    d.rectangle((30, 47, 34, 53), fill=fg)
    d.rectangle((23, 52, 41, 56), fill=fg)


def _draw_icon(state: str):
    """Desenha o ícone de um estado em 64x64 RGBA. Sem asset binário no repo."""
    bg, fg = _PALETTE.get(state, _PALETTE["idle"])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 61, 61), fill=bg)
    if state == "recording":
        d.ellipse((20, 20, 43, 43), fill=fg)
    elif state == "working":
        d.arc((15, 15, 48, 48), start=35, end=305, fill=fg, width=8)
    elif state == "error":
        d.line((20, 20, 43, 43), fill=fg, width=8)
        d.line((43, 20, 20, 43), fill=fg, width=8)
    else:
        _draw_mic(d, fg)
    return img


def _flat_icon(state: str):
    """Último recurso: quadrado chapado. Uma versão do Pillow sem `arc(width=)`
    não pode custar a bandeja inteira — sem ícone o pystray nem sobe."""
    bg = _PALETTE.get(state, _PALETTE["idle"])[0]
    return Image.new("RGBA", (64, 64), bg)


# --------------------------------------------------------------------------- #
# blindagem dos callbacks de menu
# --------------------------------------------------------------------------- #
def _menu_action(label: str):
    """Decorador OBRIGATÓRIO de todo callback de menu.

    Uma exceção que escapa para dentro do pystray derruba o ícone: o app fica
    rodando, invisível e sem nenhuma forma de ser fechado pela interface.
    O wrapper aceita 0, 1 ou 2 argumentos porque o pystray inspeciona a
    aridade do callback e chama `action()`, `action(icon)` ou `action(icon, item)`.
    """
    def deco(fn):
        def wrapper(self, icon=None, item=None):
            return self._run_action(label, lambda: fn(self))

        # De propósito SEM functools.wraps: ela deixa `__wrapped__` no wrapper e
        # inspect.signature() segue esse atributo, então o pystray leria a
        # aridade da função ORIGINAL em vez da do wrapper.
        wrapper.__name__ = getattr(fn, "__name__", "menu_action")
        wrapper.__qualname__ = getattr(fn, "__qualname__", wrapper.__name__)
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        return wrapper
    return deco


class Tray:
    """Ícone de bandeja do wisper.

    `app` é a instância de `wispr.app.App`. Tudo que a bandeja lê do app é lido
    com getattr defensivo: durante o boot (e durante um shutdown) os
    subsistemas podem simplesmente ainda não existir, e um menu que explode é
    pior do que um menu com "não detectado".
    """

    def __init__(self, app):
        self.app = app
        self.cfg = getattr(app, "cfg", None) or config.load()
        self.log = log
        self._lock = threading.RLock()
        self._state = "idle"
        self._icon = None
        self._images = {}
        self._applied = (None, None)   # (estado, tooltip) já empurrados para o shell
        self._stopped = threading.Event()

    # ----------------------------------------------------------------- API --
    def run(self) -> None:
        """BLOQUEIA até `stop()`. TEM QUE SER CHAMADO NA THREAD PRINCIPAL.

        O pystray win32 cria aqui a janela oculta do ícone e roda o próprio
        `GetMessage`/`DispatchMessage` nesta thread — é por isso que o hook de
        teclado mora numa thread separada com o pump dele. Chamar isto de uma
        thread secundária pode até subir, mas o ícone fica sem receber as
        mensagens do shell (ex.: TaskbarCreated depois de um restart do Explorer).
        """
        if threading.current_thread() is not threading.main_thread():
            self.log.warning("Tray.run() called off the main thread; pystray expects the main thread")

        # stop() pode ter chegado antes do run() (falha no boot, sinal, segunda
        # instância). Entrar no loop agora deixaria um processo sem janela e sem
        # ícone, impossível de fechar a não ser pelo Gerenciador de Tarefas.
        if self._stopped.is_set():
            self.log.info("tray not started: stop() was already requested")
            return

        if pystray is None or Image is None:
            # Degradar, nunca derrubar: sem bandeja o ditado continua funcionando.
            self.log.error("tray disabled, pystray/Pillow unavailable: %r", _UI_IMPORT_ERROR)
            self._stopped.wait()
            return

        try:
            icon = self._build_icon()
        except Exception:
            self.log.exception("failed to build tray icon; running without tray")
            self._stopped.wait()
            return

        with self._lock:
            self._icon = icon
            self._applied = (None, None)
        # Segunda checagem: o stop() que chegou enquanto o ícone era construído
        # viu _icon=None e não teve em quem bater.
        if self._stopped.is_set():
            with self._lock:
                self._icon = None
            self.log.info("tray stopped before the loop started")
            return

        self.log.info("tray starting (state=%s)", self._state)
        try:
            icon.run(setup=self._on_ready)
        except Exception:
            self.log.exception("tray message loop crashed")
        finally:
            with self._lock:
                self._icon = None
            self._stopped.set()
            self.log.info("tray loop finished")

    def stop(self) -> None:
        """Encerra o loop do pystray. Seguro de chamar de qualquer thread,
        mais de uma vez, e antes do `run()` (aí o `run()` retorna na hora)."""
        self._stopped.set()
        with self._lock:
            icon = self._icon
        if icon is None:
            return
        try:
            icon.visible = False   # NIM_DELETE já: some o ícone sem esperar o pump
        except Exception:
            self.log.debug("hiding tray icon failed", exc_info=True)
        try:
            icon.stop()
        except Exception:
            self.log.warning("icon.stop() failed", exc_info=True)

    def set_state(self, state: str) -> None:
        """Troca o ícone/tooltip. Chamado das threads worker e hook.

        Só mexe em `icon.icon` e `icon.title`, as duas coisas que o pystray
        aceita de outra thread. O menu NÃO é reconstruído aqui de propósito:
        remontar o HMENU fora da thread que faz o TrackPopupMenuEx não é
        garantido. O cabeçalho do menu é uma propriedade dinâmica e se atualiza
        sozinho na próxima vez que o menu é montado.
        Seguro antes do `run()`: o estado fica guardado e é aplicado no setup.
        """
        s = _normalize_state(state)
        if s is None:
            self.log.warning("unknown tray state %r ignored", state)
            return
        with self._lock:
            changed = s != self._state
            self._state = s
            has_icon = self._icon is not None
        if changed:
            self.log.debug("tray state -> %s", s)
        if has_icon:
            self._apply_state()

    def notify(self, title: str, msg: str) -> None:
        """Balão de notificação. Falha em silêncio de propósito: em algumas
        builds do Windows 11 (e com 'Assistente de foco' ligado) o
        Shell_NotifyIcon com NIF_INFO simplesmente estoura, e isso nunca pode
        derrubar quem chamou."""
        t = _one_line(title, _NOTIFY_TITLE_MAX)
        m = _one_line(msg, _NOTIFY_MSG_MAX)
        if not t and not m:
            return
        if not m:
            m, t = t, APP_NAME     # balão sem corpo não aparece em nada
        with self._lock:
            icon = self._icon
        if icon is None or self._stopped.is_set():
            self.log.info("notify (no tray): %s | %s", t, m)
            return
        try:
            try:
                icon.remove_notification()   # alguns shells engolem o 2o balão se o 1o ainda está de pé
            except Exception:
                pass
            icon.notify(m, t)                # atenção: pystray é notify(message, title)
        except Exception:
            self.log.warning("balloon notification failed: %s | %s", t, m, exc_info=True)

    # ------------------------------------------------------- ciclo de vida --
    def _on_ready(self, icon=None) -> None:
        """setup= do pystray: roda na thread do ícone assim que ele existe."""
        try:
            if icon is not None:
                with self._lock:
                    self._icon = icon
            if self._stopped.is_set():
                # Pediram para sair durante o setup: sair pelo caminho do pystray.
                self.log.info("tray stopped during setup")
                self.stop()
                return
            self._apply_state()
            with self._lock:
                cur = self._icon
            if cur is not None:
                cur.visible = True
                # visible=False engole o NIM_MODIFY do ícone/tooltip; reaplicar
                # agora que o ícone existe de verdade no shell.
                self._apply_state(force=True)
            self._refresh_menu()
            self.log.info("tray ready")
        except Exception:
            self.log.exception("tray setup failed")

    def _apply_state(self, force: bool = False) -> None:
        """Empurra ícone e tooltip para o shell. Só quando mudou de verdade:
        cada atribuição de `icon.icon` faz o pystray serializar a imagem em
        arquivo temporário e criar um HICON novo."""
        if self._stopped.is_set() and not force:
            return
        with self._lock:
            state = self._state
            icon = self._icon
            applied = self._applied
        if icon is None:
            return
        # Fora do lock: `_effective_state()` pergunta ao app, e a regra do
        # módulo é nunca chamar o app segurando `self._lock`.
        state = self._effective_state(state)
        tip = self._tooltip(state)
        try:
            if force or applied[0] != state:
                img = self._image(state)
                if img is not None:
                    icon.icon = img
            if force or applied[1] != tip:
                icon.title = tip
        except Exception:
            self.log.exception("failed to apply tray state %r", state)
            return
        with self._lock:
            self._applied = (state, tip)

    def _refresh_menu(self) -> None:
        """Remonta o menu. Só é chamado de dentro de um callback de menu, ou
        seja, já na thread do pystray."""
        if self._stopped.is_set():
            return
        with self._lock:
            icon = self._icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            self.log.debug("update_menu failed", exc_info=True)

    def _run_action(self, label: str, fn) -> None:
        """Executa uma ação de menu engolindo qualquer exceção (ver `_menu_action`)."""
        try:
            fn()
        except Exception:
            self.log.exception("tray action %r failed", label)
            self.notify(APP_NAME, "Falha em “%s”. Veja os logs." % label)
        finally:
            # Depois do "Sair" o ícone já foi destruído: mexer nele aqui só
            # geraria traceback no log a cada saída.
            if not self._stopped.is_set():
                try:
                    self._apply_state()
                    self._refresh_menu()
                except Exception:
                    self.log.debug("post-action refresh failed", exc_info=True)

    def _spawn(self, name: str, fn, label: str) -> None:
        """Roda `fn` fora da thread do pystray. Callback de menu que bloqueia
        congela o bombeamento de mensagens da thread principal e o Windows
        esmaece a janela do ícone. Sempre daemon: não pode segurar a saída.

        `name` nomeia a thread no log; `label` é o que o usuário lê no balão.
        """
        def body():
            try:
                fn()
            except Exception:
                self.log.exception("tray background task %r failed", name)
                self.notify(APP_NAME, "Falha em “%s”. Veja os logs." % label)

        try:
            threading.Thread(target=body, name="tray-%s" % name, daemon=True).start()
        except Exception:
            self.log.exception("could not start tray background task %r", name)
            self.notify(APP_NAME, "Falha em “%s”. Veja os logs." % label)

    # ------------------------------------------------------------- ícones ---
    def _image(self, state: str):
        with self._lock:
            img = self._images.get(state)
        if img is not None:
            return img

        path = config.ASSETS_DIR / _ICON_FILES.get(state, _ICON_FILES["idle"])
        img = None
        try:
            if path.is_file():
                with Image.open(path) as src:     # o usuário pode ter trocado o PNG
                    img = src.convert("RGBA")
        except Exception:
            self.log.warning("could not load tray icon %s, drawing the default", path, exc_info=True)
            img = None
        if img is None:
            try:
                img = _draw_icon(state)
            except Exception:
                self.log.warning("drawing the tray icon failed, using a flat one", exc_info=True)
                try:
                    img = _flat_icon(state)
                except Exception:
                    self.log.exception("no tray image could be produced for %r", state)
                    return None
            self._save_asset(img, path)

        with self._lock:
            self._images[state] = img
        return img

    def _save_asset(self, img, path: Path) -> None:
        """Grava o PNG desenhado em ASSETS_DIR na primeira vez, para o usuário
        poder substituir depois. Falhar aqui (disco cheio, pasta só-leitura)
        jamais pode impedir a bandeja de subir."""
        try:
            config.ensure_dirs()
            if not path.exists():
                img.save(path, format="PNG")
                self.log.debug("wrote tray asset %s", path)
        except Exception:
            self.log.debug("could not write tray asset %s", path, exc_info=True)

    def _tooltip(self, state: str) -> str:
        try:
            return _one_line("%s — %s · %s" % (APP_NAME, self._status_label(state),
                                               self._hotkey()), _TIP_MAX)
        except Exception:
            self.log.debug("tooltip build failed", exc_info=True)
            return APP_NAME

    # -------------------------------------------------- leitura do app -----
    def _component(self, *names):
        """Pega o primeiro subsistema que existir no app. Os nomes dos atributos
        do App não estão no contrato — só os métodos estão —, então a bandeja
        procura os candidatos em vez de assumir um."""
        for n in names:
            obj = getattr(self.app, n, None)
            if obj is not None:
                return obj
        return None

    def _hotkey(self) -> str:
        return _pretty_hotkey(self.cfg.get("hotkey", "win+a"))

    def _status_label(self, state: str) -> str:
        """Texto de estado. O App sabe mais que a bandeja (modelo carregando,
        microfone morto), então usa-se `status_text()` quando ele existir — mas
        no estado de erro vale o que a bandeja recebeu, porque o ícone vermelho
        é temporário e o App já voltou para 'idle'."""
        if state != "error":
            fn = getattr(self.app, "status_text", None)
            if callable(fn):
                try:
                    txt = _one_line(fn(), _STATUS_MAX)
                    if txt:
                        return txt
                except Exception:
                    self.log.debug("app.status_text() failed", exc_info=True)
        elif self._hook_health()[0] is False:
            # Erro por hook caído tem nome: "erro (veja os logs)" mandaria o
            # usuário para o log descobrir o que a bandeja já sabe.
            return "atalho inativo"
        if self._is_paused():
            return "pausado"
        return _STATE_LABELS.get(state, state)

    def _is_paused(self) -> bool:
        flag = getattr(self.app, "enabled", None)
        if flag is None:
            probe = getattr(self.app, "is_enabled", None)
            if callable(probe):
                flag = probe()
        return flag is False

    def _history(self) -> list:
        """Últimas transcrições, TEXTO ORIGINAL, mais recente primeiro.

        Nada de `_one_line()` aqui: este é o texto que vai para o clipboard em
        'Histórico'. Colapsar quebras de linha é coisa do rótulo do menu.
        """
        h = getattr(self.app, "history", None)
        if not h:
            return []
        try:
            items = [str(x) for x in list(h) if str(x).strip()]
        except Exception:
            self.log.debug("history read failed", exc_info=True)
            return []
        items = items[-HISTORY_SLOTS:]
        items.reverse()          # mais recente no topo; app.history cresce por append
        return items

    def _engine(self) -> str:
        return str(self.cfg.get("engine", "local") or "local").lower()

    def _hook_health(self):
        """(vivo, falhas, instalações) do HotkeyEngine.

        Os três são OPCIONAIS: uma versão do engine sem eles ainda tem que
        montar o menu, e `vivo=None` significa exatamente "não dá para saber" —
        nunca "quebrado". Sem o hook não existe Win+A, então esta é a única
        medida de saúde que importa para o usuário (ARCHITECTURE §3).
        """
        hk = self._component("hotkey_engine", "hotkey", "hotkeys", "hook")
        if hk is None:
            return None, 0, 0
        try:
            alive = getattr(hk, "alive", None)
            if callable(alive):
                # Tolera `alive()` em vez de propriedade: um método ligado é
                # sempre verdadeiro e reportaria "ativo" com o hook morto.
                alive = alive()
            return (bool(alive) if alive is not None else None,
                    _as_int(getattr(hk, "failed_installs", 0)),
                    _as_int(getattr(hk, "installs", 0)))
        except Exception:
            # Property que levanta, engine no meio da construção: "não sei"
            # jamais pode virar ícone vermelho falso.
            self.log.debug("hotkey health probe failed", exc_info=True)
            return None, 0, 0

    def _app_state(self):
        """Estado REAL do App, normalizado, ou None quando não dá para saber.

        O App fala 'transcribing' e 'injecting', que `_normalize_state()`
        traduz. Property que levanta, App sem o atributo, nome desconhecido:
        tudo isso é None — "não sei" nunca pode virar uma cor inventada.
        """
        try:
            st = getattr(self.app, "state", None)
            if callable(st):
                st = st()       # tolera `state()` em vez de property
        except Exception:
            self.log.debug("app state probe failed", exc_info=True)
            return None
        return _normalize_state(st)

    def _mic_ok(self):
        """True | False | None para a captura. Nunca levanta."""
        try:
            value = getattr(self.app, "mic_ok", None)
            if callable(value):
                value = value()
        except Exception:
            self.log.debug("mic health probe failed", exc_info=True)
            return None
        return None if value is None else bool(value)

    def _effective_state(self, state: str) -> str:
        """Estado que o ícone mostra de fato. Sobe E desce.

        Sobe: hook morto = o atalho não chega mais, e o app ficaria verde de
        "pronto" enquanto o usuário aperta Win+A no vazio. Só sobrepõe o
        repouso: 'gravando' e 'transcrevendo' são estados vivos e informativos.

        Desce: o vermelho de `App.on_hook_error()` não tem timer de volta ao
        verde de propósito — ele conta com esta função reavaliando a saúde do
        hook a cada repintura. Enquanto isto só subia, o `self._state` latchado
        em 'error' recalculava 'error' para sempre: o watchdog reinstalava o
        hook segundos depois, o Win+A voltava a funcionar e o ícone continuava
        vermelho até o app ser reiniciado.

        A descida exige prova positiva, nunca um "não sei": hook vivo,
        microfone não morto (o outro vermelho sem timer é o de captura ausente
        no boot) e um estado legível vindo do App.
        """
        alive = self._hook_health()[0]
        if state == "error":
            if alive is not True or self._mic_ok() is False:
                return "error"
            real = self._app_state()
            if real is None or real == "error":
                # App ilegível, ou ele mesmo se declarando em erro: o vermelho
                # fica. Só o hook é que a bandeja tem competência para absolver.
                return "error"
            return real
        if state != "idle":
            return state
        return "error" if alive is False else state

    # ---------------------------------------------- propriedades dinâmicas --
    def _text_prop(self, fn, fallback=""):
        """Envelopa uma propriedade de texto do menu. O pystray avalia isso
        enquanto monta o HMENU: se levantar, o menu inteiro não aparece."""
        def prop(item=None):
            try:
                return _amp(fn())
            except Exception:
                self.log.exception("tray menu text failed")
                return fallback
        return prop

    def _bool_prop(self, fn, fallback=False):
        def prop(item=None):
            try:
                return bool(fn())
            except Exception:
                self.log.exception("tray menu predicate failed")
                return fallback
        return prop

    def _header_text(self) -> str:
        with self._lock:
            state = self._state
        state = self._effective_state(state)
        return "%s — %s · %s" % (APP_NAME, self._status_label(state), self._hotkey())

    def _toggle_text(self) -> str:
        return "Retomar ditado" if self._is_paused() else "Pausar ditado"

    def _history_text(self, index: int) -> str:
        items = self._history()
        return _one_line(items[index], _ITEM_TEXT_MAX) if index < len(items) else ""

    def _history_visible(self, index: int) -> bool:
        return index < len(self._history())

    def _diag_mic(self) -> str:
        mic = self._component("mic", "audio", "microphone")
        name = getattr(mic, "name", None)
        if not name:
            return "Microfone: não detectado"
        txt = "Microfone: %s" % _one_line(name, 38)
        reopens = getattr(mic, "reopens", 0) or 0
        if reopens:
            # Reaberturas = alguém abriu o WASAPI em modo exclusivo e derrubou o
            # nosso stream compartilhado, ou o headset trocou (ARCHITECTURE §4).
            txt += " (%d reaberturas)" % reopens
        return txt

    def _diag_stt(self) -> str:
        eng = self._component("stt", "asr", "engine")
        backend = getattr(eng, "backend", None)
        if not backend:
            status = str(getattr(self.app, "engine_status", "") or "")
            if status == "loading":
                return "STT: carregando o modelo..."
            if status == "error":
                return "STT: indisponível (veja os logs)"
            backend = self._engine()
        model = getattr(eng, "model_id", None) or self.cfg.get("model_id", "")
        short = str(model).rsplit("/", 1)[-1]
        return "STT: %s · %s" % (backend, _one_line(short, 30) or "?")

    def _diag_elevated(self) -> str:
        el = _is_elevated()
        if el is None:
            return "Elevado: desconhecido"
        if el:
            return "Elevado: sim"
        # UIPI: com janela elevada em foco o hook recebe ZERO eventos e o Win+A
        # vaza para a Central de Ações. Só rodar elevado resolve (ARCHITECTURE §3).
        return "Elevado: não (falha sobre janelas admin)"

    def _diag_hook(self) -> str:
        alive, failed, installs = self._hook_health()
        if alive is None:
            # Sem `alive` só dá para relatar um fato, nunca saúde: contagem de
            # instalações bem-sucedidas não prova que o hook ainda está de pé.
            if installs:
                return "Atalho: %d instalação(ões)" % installs
            return "Atalho: sem informação"
        if not alive:
            # failed_installs conta as tentativas que o Windows recusou.
            if failed:
                return "Atalho: FALHOU (%d tentativas)" % failed
            return "Atalho: FALHOU"
        if installs > 1:
            # >1 significa que o watchdog reinstalou: o Windows despeja o hook
            # em silêncio e só o watchdog percebe.
            return "Atalho: ativo (%d instalações)" % installs
        return "Atalho: ativo"

    # -------------------------------------------------------------- menu ----
    def _build_icon(self):
        with self._lock:
            state = self._state
        img = self._image(state)
        if img is None:
            # pystray recusa Icon.visible sem imagem; melhor não ter bandeja do
            # que ter um ícone invisível que não abre menu.
            raise RuntimeError("no tray image available")
        return pystray.Icon(APP_NAME, img, self._tooltip(state), menu=self._build_menu())

    def _build_menu(self):
        try:
            return self._full_menu()
        except Exception:
            # Menu mínimo: o usuário PRECISA conseguir sair e abrir os logs.
            self.log.exception("failed to build the full tray menu; falling back to the minimal one")
            try:
                return pystray.Menu(
                    pystray.MenuItem("Abrir logs", self._on_open_logs),
                    pystray.MenuItem("Sair", self._on_quit),
                )
            except Exception:
                self.log.exception("even the minimal tray menu failed")
                return None

    def _full_menu(self):
        item = pystray.MenuItem
        menu = pystray.Menu
        sep = pystray.Menu.SEPARATOR
        return menu(
            item(self._text_prop(self._header_text, APP_NAME), None, enabled=False),
            sep,
            item(self._text_prop(self._toggle_text, "Pausar ditado"),
                 self._on_toggle_enabled,
                 checked=self._bool_prop(self._is_paused)),
            item("Colar última transcrição", self._on_repaste,
                 enabled=self._bool_prop(lambda: bool(getattr(self.app, "last_text", "")))),
            item("Histórico", menu(*self._history_items())),
            item("Motor", menu(
                item("Local (GPU)", self._on_engine_local, radio=True,
                     checked=self._bool_prop(lambda: self._engine() != "groq", True)),
                item("Groq (nuvem)", self._on_engine_groq, radio=True,
                     checked=self._bool_prop(lambda: self._engine() == "groq")),
            )),
            item("Diagnóstico", menu(
                item(self._text_prop(self._diag_mic, "Microfone: ?"), None, enabled=False),
                item(self._text_prop(self._diag_stt, "STT: ?"), None, enabled=False),
                item(self._text_prop(self._diag_elevated, "Elevado: ?"), None, enabled=False),
                item(self._text_prop(self._diag_hook, "Atalho: ?"), None, enabled=False),
            )),
            sep,
            item("Abrir configuração", self._on_open_config),
            item("Abrir logs", self._on_open_logs),
            sep,
            item("Sair", self._on_quit),
        )

    def _history_items(self) -> list:
        """Slots fixos com texto e visibilidade dinâmicos: o pystray monta o
        menu uma vez, então a quantidade de itens não pode variar — só o que
        cada um mostra e se ele aparece."""
        item = pystray.MenuItem
        items = []
        for i in range(HISTORY_SLOTS):
            items.append(item(
                self._text_prop(functools.partial(self._history_text, i), ""),
                self._history_action(i),
                visible=self._bool_prop(functools.partial(self._history_visible, i)),
            ))
        # O submenu inteiro some se NENHUM item estiver visível, por isso este
        # placeholder aparece exatamente quando o histórico está vazio.
        items.append(item("(vazio)", None, enabled=False,
                          visible=self._bool_prop(lambda: not self._history(), True)))
        return items

    def _history_action(self, index: int):
        def cb(icon=None, item=None):
            self._run_action("histórico", functools.partial(self._copy_history, index))
        return cb

    # ------------------------------------------------------------ ações -----
    def _copy_history(self, index: int) -> None:
        items = self._history()
        if index >= len(items):
            return
        text = items[index]

        def do_copy():
            # Import tardio: a ordem de import do pacote é crítica (docs/CONTRACT.md)
            # e a bandeja não precisa do inject até alguém clicar aqui.
            from wispr import inject
            inject.clip_set_text(text)
            self.notify(APP_NAME, "Copiado para a área de transferência.")

        # Fora da thread do pystray: OpenClipboard tenta de novo por até ~600 ms
        # quando outro app está segurando o clipboard, e isso na thread principal
        # travaria o bombeamento de mensagens do ícone.
        self._spawn("clipboard", do_copy, "copiar do histórico")

    @_menu_action("pausar/retomar ditado")
    def _on_toggle_enabled(self) -> None:
        self.app.toggle_enabled()

    @_menu_action("colar última transcrição")
    def _on_repaste(self) -> None:
        self.app.repaste_last()

    @_menu_action("abrir configuração")
    def _on_open_config(self) -> None:
        self.app.open_config()

    @_menu_action("abrir logs")
    def _on_open_logs(self) -> None:
        self.app.open_logs()

    @_menu_action("motor local")
    def _on_engine_local(self) -> None:
        self._set_engine("local")

    @_menu_action("motor groq")
    def _on_engine_groq(self) -> None:
        self._set_engine("groq")

    @_menu_action("sair")
    def _on_quit(self) -> None:
        # Parar a bandeja ANTES do shutdown: estamos na thread do pystray, que é
        # a principal, e se o app.shutdown() esperar por algo que espera o loop
        # da bandeja terminar, o processo trava para sempre.
        self.stop()
        # E o shutdown FORA desta thread. Ele serializa joins e locks com
        # timeout (hook 2 s, worker 2 s, PortAudio 2 s, motor 5 s, overlay 2 s):
        # até ~13 s dentro do handler de WM_COMMAND, com o ícone já removido.
        # O usuário acha que fechou, relança, e a segunda instância morre calada
        # no mutex — "o wisper não abre mais". Soltando a thread do pystray
        # agora, o `run()` retorna e o `finally` do App.run() chama shutdown()
        # de novo; ele é idempotente.
        self._spawn("shutdown", self.app.shutdown, "sair")

    def _set_engine(self, name: str) -> None:
        name = "groq" if str(name).lower() == "groq" else "local"
        label = _ENGINE_LABELS[name]
        if self._engine() == name:
            return

        self.cfg["engine"] = name
        try:
            config.save(self.cfg)
        except Exception:
            # Não é fatal: a troca continua valendo para esta sessão.
            self.log.warning("could not persist engine=%s to config.json", name, exc_info=True)

        if name == "groq" and not (self.cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY")):
            self.notify(APP_NAME, "Groq sem API key: defina groq_api_key ou GROQ_API_KEY.")

        # `App.set_engine(name)` faz a troca a quente. Ainda assim é opcional:
        # a bandeja precisa subir com um App parcialmente construído.
        fn = getattr(self.app, "set_engine", None)
        if not callable(fn):
            self.log.info("engine=%s saved; app exposes no set_engine()", name)
            self.notify(APP_NAME, "Motor %s será usado no próximo início." % label)
            return

        self.log.info("engine=%s, switching live through app.set_engine()", name)

        def work():
            # Carregar/descarregar modelo leva segundos: fazer isso na thread do
            # pystray congelaria o menu e o Windows marcaria a janela como travada.
            fn(name)
            self.notify(APP_NAME, "Motor trocado para %s agora." % label)

        self._spawn("engine", work, "trocar para %s" % label)
