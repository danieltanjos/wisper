# -*- coding: utf-8 -*-
"""Pílula flutuante (overlay) e sons de feedback do wisper.

A janela vive numa thread própria, com seu próprio `mainloop` Tk. Tk não é
thread-safe, então os métodos públicos desta classe **só escrevem num dicionário
de estado compartilhado**; um laço `root.after(33, tick)` rodando dentro da
thread Tk faz todo o desenho. Nenhum método Tk é chamado de fora dessa thread —
e o tick também nunca escreve nesse dicionário, senão ele apagaria um
`show_recording()` que outra thread acabou de pedir.

O ponto crítico é o foco. Se a pílula roubar o foco, o texto transcrito é
digitado na janela dona da pílula em vez da janela do usuário. Por isso a janela
nunca é exibida com `deiconify()`: ela nasce `withdraw()` e só aparece via
`ShowWindow(hwnd, SW_SHOWNOACTIVATE)`, com
`WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE` (mais
`WS_EX_TOPMOST`, = 0x080800A8) aplicado no HWND real.
Ver docs/ARCHITECTURE.md seção 6.

Os sons moram aqui porque são feedback de UI, não áudio de captura.

Linux (KDE Plasma 6, Wayland): o mesmo arquivo, com ramos `_WIN`. O Tk roda no
XWayland; uma janela `overrideredirect` do X11 não é gerenciada pelo KWin e
**nunca recebe foco** (não existe NOACTIVATE para pedir: ela simplesmente não
entra na política de foco), então o ex-style vira desnecessário. Mostrar e
esconder é `deiconify()`/`withdraw()` do próprio Tk — que aqui NÃO ativa nada,
ao contrário do Windows. A posição usa a área de trabalho do `_NET_WORKAREA`
(o KWin publica para o XWayland), para a pílula ficar acima do painel. Não há
`-transparentcolor` no X11: a janela é recortada no formato da cápsula pela
extensão SHAPE (`_x11_shape_capsule`), senão sobravam cantos pretos. Precisa do pacote `python3-tkinter`; sem ele o app segue
sem pílula, como sempre. Ver docs/LINUX.md.
"""
from __future__ import annotations

import array
import ctypes
import ctypes.wintypes as wt
import io
import logging
import math
import queue
import re
import threading
import subprocess
import sys
import time
import wave

try:
    import winsound
except ImportError:          # Linux: sem bipes por enquanto (`sounds` e' False por padrao)
    winsound = None

from wispr import config
from wispr import win32stub

try:  # logging_setup é de outro módulo; a ausência dele não pode derrubar nada
    from wispr import logging_setup as _logging_setup
    log = _logging_setup.get(__name__)
except Exception:  # pragma: no cover - só acontece em teste isolado do módulo
    log = logging.getLogger(__name__)

__all__ = ["Overlay", "ping_start", "ping_stop"]

# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------
# use_last_error=True guarda o GetLastError num slot do ctypes, que não é
# embaralhado pelas chamadas Win32 que o próprio ctypes faz entre a chamada e a
# leitura. Sem isso o código de erro lido é lixo.
_WIN = sys.platform == "win32"
# Fora do Windows: aceita os argtypes/restype abaixo e estoura se for CHAMADO;
# toda chamada Win32 deste arquivo ja vive dentro de try/except ou atras de _WIN.
_u32 = win32stub.dll("user32")

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

# ARCHITECTURE seção 6 registra 0x080800A8 para a janela do overlay. Os quatro
# estilos nomeados lá somam 0x080800A0; o 0x8 que falta é WS_EX_TOPMOST, que o
# Tk liga sozinho em attributes("-topmost", True). A gente inclui o bit na
# máscara de propósito: assim o valor aplicado é o valor medido mesmo se o Tk
# ignorar o -topmost, e a verificação abaixo pode conferir contra o número do
# documento em vez de contra "o que sobrou".
EX_MASK = (WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT
           | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
EX_DOC = 0x080800A8
# Sem estes dois a pílula rouba o foco ou come cliques: é melhor não ter overlay.
EX_FATAL = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SM_CXSCREEN, SM_CYSCREEN = 0, 1
GA_ROOT = 2
MONITOR_DEFAULTTOPRIMARY = 1
MDT_EFFECTIVE_DPI = 0

_LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

# restype explícito é obrigatório em tudo que devolve HANDLE/HWND: sem isso o
# ctypes assume c_int e trunca o handle de 64 bits. É o mesmo tropeço medido em
# GetModuleHandleW na seção 3 da ARCHITECTURE, que fazia SetWindowsHookExW
# falhar com erro 126.
_u32.GetParent.restype = wt.HWND
_u32.GetParent.argtypes = [wt.HWND]
_u32.GetAncestor.restype = wt.HWND
_u32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
_u32.ShowWindow.restype = wt.BOOL
_u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u32.SetWindowPos.restype = wt.BOOL
_u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
_u32.GetSystemMetrics.restype = ctypes.c_int
_u32.GetSystemMetrics.argtypes = [ctypes.c_int]
_u32.MonitorFromPoint.restype = wt.HANDLE
_u32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]

try:  # x64 exporta ...LongPtrW; x86 só tem ...LongW. Falhar aqui mataria o import.
    _get_long = _u32.GetWindowLongPtrW
    _set_long = _u32.SetWindowLongPtrW
except AttributeError:  # pragma: no cover - só em Python 32 bits
    _get_long = _u32.GetWindowLongW
    _set_long = _u32.SetWindowLongW
_get_long.restype = _LONG_PTR
_get_long.argtypes = [wt.HWND, ctypes.c_int]
_set_long.restype = _LONG_PTR
_set_long.argtypes = [wt.HWND, ctypes.c_int, _LONG_PTR]

_shcore_dll = None
_shcore_tried = False


def _shcore():
    """shcore.dll carregada uma vez só (não existe antes do Windows 8.1)."""
    global _shcore_dll, _shcore_tried
    if not _shcore_tried:
        _shcore_tried = True
        try:
            _shcore_dll = ctypes.WinDLL("shcore", use_last_error=True)
        except Exception:
            _shcore_dll = None
            log.debug("shcore.dll indisponivel", exc_info=True)
    return _shcore_dll


def _win_err(where: str) -> None:
    """Loga o GetLastError real de uma chamada que já foi identificada como falha."""
    try:
        code = ctypes.get_last_error()
        if code:
            log.warning("overlay: %s falhou, GetLastError=%d", where, code)
    except Exception:
        pass


_dpi_mode = None


def _set_dpi_awareness() -> str:
    """Torna o processo DPI-aware antes de existir qualquer janela Tk.

    Sem isso o Windows estica a janela por bitmap num monitor escalado (pílula
    borrada) e `GetSystemMetrics` devolve pixels lógicos, o que desloca a
    posição. Tudo aqui é defensivo: se o processo já tiver awareness definida
    (pelo manifesto do python.org, ou por quem chamou antes), as chamadas falham
    com ERROR_ACCESS_DENIED e isso é esperado — o processo continua aware.

    É process-wide e idempotente; app.py pode chamar no boot, antes do pystray.
    """
    global _dpi_mode
    if _dpi_mode is not None:
        return _dpi_mode
    mode = "none"
    try:
        fn = _u32.SetProcessDpiAwarenessContext
        fn.restype = wt.BOOL
        fn.argtypes = [ctypes.c_void_p]
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Win10 1703+)
        if fn(ctypes.c_void_p(-4)):
            mode = "per-monitor-v2"
    except Exception:
        log.debug("SetProcessDpiAwarenessContext indisponivel", exc_info=True)
    if mode == "none":
        try:
            sh = _shcore()
            if sh is not None and sh.SetProcessDpiAwareness(2) == 0:  # PER_MONITOR
                mode = "per-monitor"
        except Exception:
            log.debug("SetProcessDpiAwareness indisponivel", exc_info=True)
    if mode == "none":
        try:
            if _u32.SetProcessDPIAware():
                mode = "system"
        except Exception:
            log.debug("SetProcessDPIAware indisponivel", exc_info=True)
    _dpi_mode = mode
    return mode


def _toplevel_hwnd(child: int) -> int:
    """HWND do toplevel real, que é quem carrega os ex-styles.

    `winfo_id()` devolve a janela-filha do Tk; o wrapper é o pai. Usamos
    GetAncestor(GA_ROOT), que sobe **só a cadeia de pais** — GetParent devolveria
    o *owner* de uma janela WS_POPUP, e aí o ex-style iria para a janela errada.
    """
    best = int(child)
    try:
        anc = _u32.GetAncestor(wt.HWND(child), GA_ROOT)
        if anc:
            best = int(anc)
    except Exception:
        log.debug("overlay: GetAncestor falhou", exc_info=True)
    try:
        par = _u32.GetParent(wt.HWND(child))
        if par and int(par) != best:
            log.warning("overlay: GetParent=0x%X difere de GA_ROOT=0x%X; usando GA_ROOT",
                        int(par), best)
    except Exception:
        pass
    return best


def _dpi_for(hwnd) -> int:
    """DPI efetivo da janela, com queda para o DPI do sistema e para 96."""
    if hwnd:
        try:
            fn = _u32.GetDpiForWindow
            fn.restype = wt.UINT
            fn.argtypes = [wt.HWND]
            d = int(fn(wt.HWND(hwnd)))
            if d > 0:
                return d
        except Exception:
            log.debug("GetDpiForWindow indisponivel", exc_info=True)
    try:
        fn = _u32.GetDpiForSystem
        fn.restype = wt.UINT
        fn.argtypes = []
        d = int(fn())
        if d > 0:
            return d
    except Exception:
        log.debug("GetDpiForSystem indisponivel", exc_info=True)
    return 96


def _dpi_primary(hwnd=None) -> int:
    """DPI do monitor PRIMÁRIO.

    Toda a geometria da pílula é calculada em cima de SM_CXSCREEN/SM_CYSCREEN,
    que são do primário. Perguntar o DPI da janela daria o DPI de *outro*
    monitor enquanto ela ainda está na posição default, e a primeira montagem
    sairia com a escala errada. O ponto (0,0) é sempre do primário.
    """
    try:
        hmon = _u32.MonitorFromPoint(wt.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        sh = _shcore()
        if hmon and sh is not None:
            fn = sh.GetDpiForMonitor
            fn.restype = ctypes.c_long   # HRESULT; 0 = S_OK
            fn.argtypes = [wt.HANDLE, ctypes.c_int,
                           ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT)]
            dx, dy = wt.UINT(0), wt.UINT(0)
            if fn(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                if dx.value > 0:
                    return int(dx.value)
    except Exception:
        log.debug("GetDpiForMonitor indisponivel", exc_info=True)
    return _dpi_for(hwnd)


def _primary_rect(fallback_w: int, fallback_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) do monitor PRIMÁRIO em pixels físicos (já somos DPI-aware)."""
    if not _WIN:
        return _x11_primary(fallback_w, fallback_h)
    try:
        w = int(_u32.GetSystemMetrics(SM_CXSCREEN))
        h = int(_u32.GetSystemMetrics(SM_CYSCREEN))
        if w > 0 and h > 0:
            return 0, 0, w, h            # no Windows o primário sempre começa em 0,0
    except Exception:
        log.debug("GetSystemMetrics falhou", exc_info=True)
    return 0, 0, fallback_w, fallback_h


_X11_PRIMARY: dict = {}


def _x11_primary(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Linux: (x, y, w, h) do monitor primário em coordenadas X, pelo `xrandr`.

    A tela X é a união dos monitores (medido: 3672x1920 com o notebook
    `2592x1458+1080+0` ao lado de um monitor em pé), e centralizar nela jogava a
    pílula no meio do conjunto, fora do centro do primário. Guardado por tamanho
    da tela X: o check de tela roda a cada 0,5 s e só monitor entrando/saindo ou
    troca de escala mudam esse tamanho.
    shortcut: trocar o primário sem mudar o tamanho da tela X só é visto ao reiniciar.
    """
    key = (screen_w, screen_h)
    if key not in _X11_PRIMARY:
        rect = (0, 0, screen_w, screen_h)
        try:
            out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                                 text=True, timeout=1.0, check=False).stdout
            # " 0: +*eDP-1 2592/344x1458/193+1080+0  eDP-1": o `*` marca o primário
            m = re.search(r"\+\*\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", out)
            if m:
                w, h, x, y = map(int, m.groups())
                rect = (x, y, w, h)
        except Exception:
            log.debug("overlay: xrandr ilegivel; usando a tela X inteira", exc_info=True)
        _X11_PRIMARY.clear()
        _X11_PRIMARY[key] = rect
    return _X11_PRIMARY[key]


SPI_GETWORKAREA = 0x0030


_X11_WORKAREA = {"t": 0.0, "bottom": None}


def _x11_workarea_bottom(fallback: int) -> int:
    """Linux: base da área de trabalho pelo `_NET_WORKAREA` do X root.

    O KWin publica a área livre de painéis para os clientes XWayland (medido:
    `0, 0, 1920, 1034` com o painel de 46 px embaixo). Lido pelo `xprop`
    (pacote do Fedora KDE) e guardado por 5 s: o layout chama isto a cada troca
    de modo, e um subprocesso por quadro seria caro.

    A área vem em px LÓGICOS, mas as janelas X vivem em px multiplicados pelo
    `[Xwayland] Scale` do kwinrc (medido: 1034 com escala 1,35, notebook com a
    base em 1458). `fallback` é a base do primário e também o teto.
    """
    now = time.monotonic()
    if _X11_WORKAREA["bottom"] is not None and now - _X11_WORKAREA["t"] < 5.0:
        return _X11_WORKAREA["bottom"]
    bottom = fallback
    try:
        out = subprocess.run(["xprop", "-root", "_NET_WORKAREA"], capture_output=True,
                             text=True, timeout=1.0, check=False).stdout
        nums = [int(x) for x in out.split("=", 1)[1].replace(",", " ").split()[:4]]
        if len(nums) == 4 and nums[3] > 0:
            k = subprocess.run(["kreadconfig6", "--file", "kwinrc", "--group", "Xwayland",
                                "--key", "Scale", "--default", "1"], capture_output=True,
                               text=True, timeout=1.0, check=False).stdout.strip()
            bottom = min(fallback, int(round((nums[1] + nums[3]) * float(k or 1))))
    except Exception:
        log.debug("overlay: _NET_WORKAREA ilegivel; usando a base da tela", exc_info=True)
    _X11_WORKAREA.update(t=now, bottom=bottom)
    return bottom


_XSHAPE: dict = {}


def _capsule_rows(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Faixas de 1 px de altura (x, y, w, h) que cobrem a cápsula `w`x`h`."""
    r = h / 2.0
    rows = []
    for y in range(h):
        dy = y + 0.5 - r
        dx = math.sqrt(max(0.0, r * r - dy * dy))
        x0, x1 = int(round(r - dx)), int(round(w - r + dx))
        if x1 > x0:
            rows.append((x0, y, x1 - x0, 1))
    return rows


def _xlib():
    """libX11/libXext por ctypes e uma conexão X própria, só da thread Tk. None se faltar."""
    if "dpy" not in _XSHAPE:
        _XSHAPE["dpy"] = None            # falhou uma vez, não tenta a cada quadro
        try:
            P, U = ctypes.c_void_p, ctypes.c_ulong
            x11, xext = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXext.so.6")
            x11.XOpenDisplay.restype = P
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XFlush.argtypes = [P]
            x11.XFree.argtypes = [P]
            x11.XQueryTree.argtypes = [P, U, ctypes.POINTER(U), ctypes.POINTER(U),
                                       ctypes.POINTER(P), ctypes.POINTER(ctypes.c_uint)]
            xext.XShapeCombineRectangles.argtypes = [P, U, ctypes.c_int, ctypes.c_int,
                                                     ctypes.c_int, P, ctypes.c_int,
                                                     ctypes.c_int, ctypes.c_int]
            dpy = x11.XOpenDisplay(None)
            if dpy:
                _XSHAPE.update(dpy=dpy, x11=x11, xext=xext)
        except Exception:
            log.warning("overlay: sem extensao SHAPE; pilula sai retangular", exc_info=True)
    return _XSHAPE if _XSHAPE["dpy"] else None


def _x11_wrapper(win: int) -> int | None:
    """O pai do `winfo_id()`: o wrapper do Tk, a janela de topo que o KWin compõe.

    `wm frame` não serve: sem reparent (override-redirect) ele devolve o próprio
    `winfo_id()` (medido). O wrapper só existe depois do primeiro update_idletasks.
    """
    x = _xlib()
    if x is None:
        return None
    root, parent = ctypes.c_ulong(), ctypes.c_ulong()
    kids, n = ctypes.c_void_p(), ctypes.c_uint()
    if not x["x11"].XQueryTree(x["dpy"], win, ctypes.byref(root), ctypes.byref(parent),
                               ctypes.byref(kids), ctypes.byref(n)):
        return None
    if kids:
        x["x11"].XFree(kids)
    return parent.value or None


def _x11_shape_capsule(win: int, w: int, h: int) -> None:
    """Linux: recorta a janela X no formato da cápsula (extensão SHAPE).

    Sem `-transparentcolor` no X11 o fundo da janela aparecia como cantos
    pretos em volta da pílula. A máscara é de 1 bit, como o desenho do Tk
    (que no X11 não tem antialias).
    """
    x = _xlib()
    if x is None:
        return
    rows = _capsule_rows(w, h)
    flat = [v for row in rows for v in row]
    arr = (ctypes.c_short * len(flat))(*flat)       # XRectangle = 4 shorts
    # ShapeBounding=0, ShapeSet=0, Unsorted=0
    x["xext"].XShapeCombineRectangles(x["dpy"], win, 0, 0, 0, arr, len(rows), 0, 0)
    x["x11"].XFlush(x["dpy"])


def _workarea_bottom(fallback: int) -> int:
    """Borda de cima da barra de tarefas do monitor primário, em px físicos.

    A pílula fica ancorada nela, e não na base da tela: assim "um pouquinho
    acima da barra" vale com a barra de qualquer altura ou escondida.
    """
    if not _WIN:
        return _x11_workarea_bottom(fallback)
    try:
        rc = wt.RECT()
        if _u32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0) and rc.bottom > 0:
            return int(rc.bottom)
    except Exception:
        log.debug("SystemParametersInfoW(SPI_GETWORKAREA) falhou", exc_info=True)
    return fallback


# --------------------------------------------------------------------------
# Paleta e métricas (em px lógicos @96 dpi; multiplicadas pela escala de DPI)
# --------------------------------------------------------------------------
# Magenta é a cor-chave do -transparentcolor: cada pixel dessa cor vira um
# buraco 100% transparente na janela. Nada do desenho pode usá-la.
_KEY = "#ff00ff"
# A Flow Bar do Wispr Flow, copiada do video do site (2026-09-17): pilula preta
# com contorno claro fino, 11 barras brancas de pontas redondas que descansam
# como pontos, e um traco cinza minusculo no lugar dela quando ocioso.
_BG = "#000000"
_EDGE = "#e9e6dc"
_BAR = "#ffffff"
_SPIN = "#ffffff"
_TXT = "#ffffff"
_IDLE = "#6f6f6f"

_BASE_W = 84
_BASE_H = 30
_NBARS = 11
_BAR_W = 2
_BAR_GAP = 3
_FONT_PX = 11
_SPIN_R = 8
_IDLE_W = 36
_IDLE_H = 6
_ANIM_S = 0.26        # transicao de tamanho entre modos
_SLIDE_S = 0.4        # entra subindo de baixo da tela (rapido -> freia), sai ao contrario

_LABEL_WORK = "transcrevendo…"
_TICK_MS = 16         # 60 fps; o tick reagenda por PRAZO, nao por "16 ms depois de terminar"
_MSG_MAX_CHARS = 200


def _load_cfg() -> config.Config:
    try:
        return config.load()
    except Exception:
        log.exception("config.load falhou; usando DEFAULTS")
        return config.Config(dict(config.DEFAULTS))


def _norm_level(rms) -> float:
    """RMS linear -> 0..1 numa escala em dB.

    Voz de ditado fica entre -45 e -12 dBFS; em escala linear as barras quase
    não saem do chão. -60 dBFS vira 0 e -6 dBFS vira 1.
    """
    try:
        v = float(rms)
        if not math.isfinite(v) or v <= 0.0:
            return 0.0
        db = 20.0 * math.log10(max(v, 1e-7))
    except Exception:
        # numpy array com mais de um elemento, Decimal, None, int gigante...
        return 0.0
    return max(0.0, min(1.0, (db + 60.0) / 54.0))


class _NullOverlay:
    """Overlay desligado: cumpre a API inteira e não faz nada.

    Existe para o app nunca precisar de `if cfg.overlay:` espalhado no caminho
    quente. `Overlay(cfg)` devolve uma instância disto quando cfg.overlay é
    False, e a instância real vira isto por dentro se o Tk não subir.
    """

    alive = False

    def start(self):
        return self

    def show_recording(self):
        pass

    def show_transcribing(self):
        pass

    def show_message(self, text: str, ms: int = 1800):
        pass

    def set_level(self, rms: float):
        pass

    def hide(self):
        pass

    def stop(self):
        pass


class Overlay:
    """Pílula flutuante: barras de nível ao gravar, spinner ao transcrever,
    texto curto para erros.

    Todos os métodos públicos podem ser chamados de qualquer thread: eles só
    gravam chaves num dict. Quem desenha é o `_tk_tick` dentro da thread Tk.
    """

    def __new__(cls, cfg=None):
        c = cfg if cfg is not None else _load_cfg()
        try:
            enabled = bool(c.get("overlay", True))
        except Exception:
            enabled = True
        if not enabled:
            return _NullOverlay()
        self = super().__new__(cls)
        self._cfg0 = c  # evita um segundo config.load() dentro de __init__
        return self

    def __init__(self, cfg=None):
        base = getattr(self, "_cfg0", None)
        self.cfg = base if base is not None else (cfg if cfg is not None else _load_cfg())
        self.alive = False
        self._dead = False
        self._thread = None
        self._ready = threading.Event()
        # Estado compartilhado entre threads. Escrita de chave em dict é atômica
        # sob a GIL, e é o único canal que temos para falar com a thread Tk.
        # Só os métodos públicos escrevem aqui; o tick apenas lê.
        # Traco cinza ocioso, como a Flow Bar. `overlay_idle_pill: false` esconde.
        try:
            self._idle = bool(self.cfg.get("overlay_idle_pill", False))
        except Exception:
            self._idle = False
        self._st = {
            "mode": "idle" if self._idle else "",   # "" | "idle" | "rec" | "work" | "msg"
            "text": "",
            "level": 0.0,
            "visible": self._idle,
            "msg_until": 0.0,
            "quit": False,
        }
        # --- daqui para baixo, tudo só é tocado pela thread Tk ---
        self._tkmod = None
        self._root = None
        self._cv = None
        self._font = None
        self._hwnd = None
        self._key = _KEY      # vira _BG se o -transparentcolor não pegar
        self._xwin = None     # Linux: janela X de fora do Tk (recebe o recorte SHAPE)
        self._vis = False
        self._frame = 0
        self._lvl = 0.0
        self._lay_key = None
        self._geom = None
        self._anim = None     # (frame0, w0, h0, w1, h1) durante a transicao de tamanho
        self._slide = None    # (t0, +1 entrando | -1 saindo) durante o deslize vertical
        self._next_t = 0.0    # prazo do proximo tick (monotonic)
        self._scale = 1.0
        self._dpi = 96
        self._sx = 0          # origem do monitor primario (0,0 no Windows)
        self._sy = 0
        self._sw = 1920
        self._sh = 1080
        self._h = _BASE_H
        self._base_w = _BASE_W
        self._bw = _BAR_W
        self._gap = _BAR_GAP
        self._bars_w = _NBARS * _BAR_W + (_NBARS - 1) * _BAR_GAP
        self._spin_r = _SPIN_R
        self._bar_min = 1.5
        self._bar_max = _BASE_H / 2.0 - 4.0
        self._bars = []
        self._bar_x = []
        self._bar_a = [1.5] * _NBARS
        self._bar_shape = [1.0] * _NBARS
        self._arc = None
        self._label = None

    # ---------------- API pública (qualquer thread) ----------------

    def start(self) -> "Overlay":
        """Sobe a thread Tk e espera ela reportar sucesso ou falha."""
        if self._dead or self._thread is not None:
            return self
        self._st["quit"] = False   # um stop() antes do start() não pode matar o boot
        try:
            self._thread = threading.Thread(target=self._tk_main, name="overlay", daemon=True)
            self._thread.start()
        except Exception:
            log.exception("nao foi possivel subir a thread do overlay; seguindo sem UI")
            self._dead = True
            self._thread = None
            return self
        if not self._ready.wait(5.0):
            # Não é motivo para desistir: se o Tk aparecer depois, os métodos já
            # estarão escrevendo no dict e ele pega o estado no primeiro tick.
            log.warning("overlay demorou mais de 5s para ficar pronto")
        if self._dead:
            log.warning("overlay degradado para no-op; o app continua ditando sem UI")
        return self

    def show_recording(self) -> None:
        st = self._st
        # Ordem importa: quem lê é outra thread, então tudo que o modo "rec"
        # consome já tem que estar no dict quando "mode" virar "rec".
        st["level"] = 0.0
        st["text"] = ""
        st["msg_until"] = 0.0
        st["mode"] = "rec"
        st["visible"] = True

    def show_transcribing(self) -> None:
        st = self._st
        st["text"] = ""
        st["msg_until"] = 0.0
        st["mode"] = "work"
        st["visible"] = True

    def show_message(self, text: str, ms: int = 1800) -> None:
        st = self._st
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            ms = 1800
        s = "" if text is None else str(text)
        # create_text quebra linha em "\n" mas Font.measure só mede uma linha:
        # com quebra, a pílula sairia estreita e o texto vazaria para fora dela.
        s = " ".join(s.split())
        if len(s) > _MSG_MAX_CHARS:
            s = s[:_MSG_MAX_CHARS - 1] + "…"
        st["text"] = s
        st["msg_until"] = time.monotonic() + max(0.2, ms / 1000.0)
        st["mode"] = "msg"
        st["visible"] = True

    def set_level(self, rms: float) -> None:
        # Sem validação de propósito: é chamado ~30x/s pela thread de nível, e
        # quem normaliza (e engole lixo) é o _norm_level, já na thread Tk.
        self._st["level"] = rms

    def hide(self) -> None:
        """Fim de ditado: volta ao traco ocioso (ou some, sem ele)."""
        st = self._st
        st["msg_until"] = 0.0
        st["mode"] = "idle" if self._idle else ""
        st["visible"] = self._idle

    def stop(self) -> None:
        self._st["quit"] = True
        self._st["visible"] = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(2.0)
            if t.is_alive():
                # Tk travou. Esconder daqui é assíncrono para janela de outra
                # thread e pode não chegar, mas é melhor que deixar a pílula
                # encalhada por cima de tudo até o processo morrer.
                log.warning("overlay: thread Tk nao encerrou em 2s; escondendo pelo Win32")
                self._hide_now()
        self.alive = False

    # ---------------- thread Tk ----------------

    def _hide_now(self) -> None:
        """Esconde pelo Win32. Nunca levanta."""
        if not _WIN:
            # Tk nao e' thread-safe e no X11 nao ha ShowWindow assincrono: so a
            # thread Tk esconde. O stop() de outra thread (Tk travado) desiste.
            try:
                if self._root is not None and threading.current_thread() is self._thread:
                    self._root.withdraw()
            except Exception:
                log.debug("overlay: withdraw falhou", exc_info=True)
            self._vis = False
            return
        try:
            hwnd = self._hwnd
            if hwnd:
                _u32.ShowWindow(wt.HWND(hwnd), SW_HIDE)
        except Exception:
            log.debug("overlay: ShowWindow(SW_HIDE) falhou", exc_info=True)
        self._vis = False

    def _tk_main(self) -> None:
        """Ponto de entrada da thread Tk. Qualquer falha aqui degrada o overlay
        para no-op — sob pythonw.exe uma exceção solta é invisível."""
        try:
            import tkinter as tk
            import tkinter.font as tkfont

            mode = _set_dpi_awareness()
            log.info("overlay dpi awareness=%s", mode)
            try:
                # O timer do Windows tem 15,6 ms de resolucao por padrao: um
                # after(16) vira 16 ou 31 ms ao acaso e o quadro "gagueja".
                # 1 ms so' para este processo; o SO reverte quando ele sai.
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
            except Exception:
                log.debug("timeBeginPeriod(1) falhou", exc_info=True)

            self._tkmod = tk
            root = tk.Tk()
            root.withdraw()                      # nunca usar deiconify() depois: ele ATIVA
            root.overrideredirect(True)          # sem barra de título, sem borda
            root.attributes("-topmost", True)
            try:
                root.wm_attributes("-transparentcolor", _KEY)
            except Exception:
                # Sem cor-chave a pílula fica um retângulo opaco: feio, mas
                # utilizável. Degradar é melhor que ficar sem overlay nenhum.
                self._key = _BG
                log.warning("overlay: -transparentcolor recusado; pilula sai retangular",
                            exc_info=True)
            root.configure(bg=self._key)
            self._root = root

            self._cv = tk.Canvas(root, width=_BASE_W, height=_BASE_H, bg=self._key,
                                 highlightthickness=0, borderwidth=0)
            self._cv.pack()

            # O wrapper Win32 do Tk só existe depois disto; sem o update_idletasks
            # o winfo_id() ainda não tem pai e o ex-style iria para a janela errada.
            root.update_idletasks()
            if not _WIN:
                self._tk_main_x11(root, tkfont)
                return
            hwnd = _toplevel_hwnd(root.winfo_id())
            self._hwnd = hwnd

            ex = int(_get_long(wt.HWND(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF
            ctypes.set_last_error(0)
            prev = int(_set_long(wt.HWND(hwnd), GWL_EXSTYLE, ex | EX_MASK)) & 0xFFFFFFFF
            if prev == 0 and ex != 0:
                # SetWindowLong devolve 0 tanto em erro quanto quando o valor
                # anterior era 0: só dá para distinguir zerando o last-error antes.
                _win_err("SetWindowLong(GWL_EXSTYLE)")
            got = int(_get_long(wt.HWND(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF
            log.info("overlay hwnd=0x%X exstyle=0x%08X (medido em ARCHITECTURE: 0x%08X)",
                     hwnd, got, EX_DOC)
            missing = EX_FATAL & ~got
            if missing:
                # Sem NOACTIVATE a pílula rouba o foco e o ditado é digitado
                # dentro dela em vez da janela do usuário.
                log.error("overlay sem NOACTIVATE/TRANSPARENT (0x%08X, faltando 0x%08X)",
                          got, missing)
                raise RuntimeError("ex-style nao aplicado")
            if (got & EX_DOC) != EX_DOC:
                # LAYERED/TOOLWINDOW/TOPMOST faltando não é fatal: no máximo a
                # pílula aparece no Alt+Tab ou perde a transparência.
                log.warning("overlay: ex-style 0x%08X difere do medido 0x%08X", got, EX_DOC)

            self._dpi = _dpi_primary(hwnd)
            self._sx, self._sy, self._sw, self._sh = _primary_rect(
                root.winfo_screenwidth(), root.winfo_screenheight())
            self._tk_metrics(tkfont)

            self.alive = True
            self._ready.set()
            self._next_t = time.monotonic()
            root.after(_TICK_MS, self._tk_tick)
            root.mainloop()
            log.info("overlay mainloop encerrado")
        except Exception:
            log.exception("overlay falhou ao iniciar; degradando para no-op")
            self._dead = True
        finally:
            self.alive = False
            self._ready.set()
            self._hide_now()
            root, self._root = self._root, None
            self._cv = None
            # Solta a fonte nomeada AQUI, na thread Tk: o __del__ de tkinter.font
            # fala com o Tcl, e ele não pode acontecer numa coleta de lixo de
            # outra thread com o interpretador ainda vivo.
            self._font = None
            if root is not None:
                try:
                    # Sem destroy() o interpretador Tcl e a janela sobrevivem à
                    # thread e só são liberados numa coleta de lixo qualquer.
                    root.destroy()
                except Exception:
                    log.debug("overlay: destroy no shutdown falhou", exc_info=True)
            self._hwnd = None

    def _tk_main_x11(self, root, tkfont) -> None:
        """Linux: o resto do `_tk_main` sem ex-style nem HWND. Roda o mainloop.

        Override-redirect no XWayland: o KWin nao gerencia a janela, ela nao
        entra no Alt+Tab, nao tem decoracao e nao recebe foco. `-topmost`
        reforca o empilhamento por cima do painel.
        """
        try:
            # tk scaling = pixels por ponto; 96 dpi = 1.333. O XWayland do KDE
            # reflete a escala configurada no Xft.dpi.
            self._dpi = max(72, int(round(float(root.winfo_fpixels("1i")))))
        except Exception:
            self._dpi = 96
        self._sx, self._sy, self._sw, self._sh = _primary_rect(
            root.winfo_screenwidth(), root.winfo_screenheight())
        self._xwin = _x11_wrapper(root.winfo_id())
        log.info("overlay x11 (XWayland) dpi=%s primario=%sx%s+%s+%s wrapper=%s",
                 self._dpi, self._sw, self._sh, self._sx, self._sy,
                 hex(self._xwin) if self._xwin else None)
        self._tk_metrics(tkfont)
        self.alive = True
        self._ready.set()
        self._next_t = time.monotonic()
        root.after(_TICK_MS, self._tk_tick)
        root.mainloop()
        log.info("overlay mainloop encerrado")

    def _tk_metrics(self, tkfont=None) -> None:
        """Recalcula tudo que depende da escala de DPI. Só na thread Tk."""
        self._scale = max(0.75, min(4.0, self._dpi / 96.0))
        s = self._scale
        self._h = int(round(_BASE_H * s))
        self._base_w = int(round(_BASE_W * s))
        self._bw = max(2, int(round(_BAR_W * s)))
        self._gap = max(2, int(round(_BAR_GAP * s)))
        self._bars_w = _NBARS * self._bw + (_NBARS - 1) * self._gap
        self._spin_r = max(5, int(round(_SPIN_R * s)))
        self._bar_min = max(1.5, 1.5 * s)
        self._bar_max = max(self._bar_min + 2.0, self._h / 2.0 - max(6.0, 7.0 * s))
        self._idle_w = int(round(_IDLE_W * s))
        self._idle_h = max(4, int(round(_IDLE_H * s)))
        # perfil de altura: barras do meio mais altas, como num medidor real
        self._bar_shape = [0.55 + 0.45 * math.cos((i - (_NBARS - 1) / 2.0) * math.pi / _NBARS)
                           for i in range(_NBARS)]
        self._bar_a = [self._bar_min] * _NBARS
        # tamanho NEGATIVO = pixels. Em pontos, o Tk aplicaria a escala do
        # sistema por cima da nossa e a fonte sairia grande demais.
        px = -max(9, int(round(_FONT_PX * s)))
        if self._font is None:
            if tkfont is None:
                import tkinter.font as tkfont  # noqa: PLC0415
            self._font = tkfont.Font(family="Segoe UI", size=px)
        else:
            # configure() em vez de uma Font nova: cada Font é um recurso nomeado
            # no Tcl, e trocar de monitor várias vezes vazaria uma por troca.
            self._font.configure(size=px)
        self._lay_key = None  # força um relayout

    def _tk_fit(self, text: str, max_px: int) -> str:
        """Encurta o texto com reticências até caber em max_px."""
        if not text:
            return ""
        try:
            f = self._font
            if f is None:
                return text
            if f.measure(text) <= max_px:
                return text
            # busca binária: medir char a char seriam centenas de chamadas Tcl
            # dentro do frame de 33 ms.
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if f.measure(text[:mid] + "…") <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            return (text[:lo] + "…") if lo > 0 else "…"
        except Exception:
            log.debug("overlay: Font.measure falhou", exc_info=True)
            return text[:40]

    def _tk_layout(self, mode: str, text: str, size=None) -> tuple[int, int]:
        """Redesenha a cápsula inteira e reposiciona a janela. Só na thread Tk.

        `size=(W, H)` é o quadro intermediário da transição: desenha só a
        cápsula nesse tamanho, sem conteúdo e sem gravar a chave. Devolve o
        tamanho FINAL do modo, que é o alvo da animação.
        """
        # A chave é gravada com o texto ORIGINAL, que é o que o tick compara.
        # Gravar o texto já truncado faria a comparação falhar em todo frame e
        # a pílula seria redesenhada e reposicionada 30x por segundo.
        key = (mode, text, self._scale, self._sw, self._sh)

        cv, s = self._cv, self._scale
        H = self._idle_h if mode == "idle" else self._h
        r = H // 2

        max_inner = max(60, int(self._sw * 0.8) - 2 * r)
        if mode == "msg":
            text = self._tk_fit(text, max_inner)
        tw = self._font.measure(text) if (text and self._font is not None) else 0

        if mode == "idle":
            W = self._idle_w
        elif mode == "work":
            # Enter: a pilula encolhe na horizontal e fica so' a rodinha.
            W = H + 2 * self._spin_r
        else:
            inner = tw if mode == "msg" else self._bars_w
            W = max(self._base_w, inner + 2 * r)
            W = min(W, max(self._base_w, int(self._sw * 0.9)))
        target = (W, H)
        if size is not None:
            W, H = int(size[0]), int(size[1])
            r = H // 2

        cv.delete("all")
        cv.configure(width=W, height=H)
        self._bars, self._bar_x, self._arc, self._label = [], [], None, None

        # Tk não tem retângulo arredondado: a cápsula é dois círculos + um
        # retângulo. A borda é uma cápsula clara com outra escura por cima.
        # Ociosa: um traço cinza chapado, sem contorno, como a Flow Bar.
        edge, fill = (_IDLE, _IDLE) if mode == "idle" else (_EDGE, _BG)
        if self._xwin:
            # Pixel que sobrar dentro do recorte SHAPE vira contorno, nao preto.
            cv.configure(bg=edge)
        b = 0 if mode == "idle" else max(1, int(round(1.5 * s)))
        cv.create_oval(0, 0, H, H, fill=edge, outline="")
        cv.create_oval(W - H, 0, W, H, fill=edge, outline="")
        cv.create_rectangle(r, 0, W - r, H, fill=edge, outline="")
        if b and H > 2 * b + 1:
            cv.create_oval(b, b, H - b, H - b, fill=fill, outline="")
            cv.create_oval(W - H + b, b, W - b, H - b, fill=fill, outline="")
            cv.create_rectangle(r, b, W - r, H - b, fill=fill, outline="")

        cy = H / 2.0
        if size is not None:
            pass                                   # quadro da transicao: so' a capsula
        elif mode == "work":
            rs = self._spin_r
            gx = W / 2.0 - rs
            self._arc = cv.create_arc(gx, cy - rs, gx + 2 * rs, cy + rs,
                                      start=0, extent=110, style=self._tkmod.ARC,
                                      outline=_SPIN, width=max(2, int(round(2.5 * s))))
        elif mode == "rec":
            # Barras como LINHAS com ponta redonda: e' o que da' o visual de
            # pilulinhas do Wispr; em repouso viram pontos. `x` e' o centro.
            x0 = (W - self._bars_w) // 2 + self._bw / 2.0
            for i in range(_NBARS):
                x = x0 + i * (self._bw + self._gap)
                self._bar_x.append(x)
                self._bars.append(cv.create_line(
                    x, cy - self._bar_min, x, cy + self._bar_min,
                    fill=_BAR, width=self._bw, capstyle="round"))
            self._bar_a = [self._bar_min] * _NBARS
        elif mode == "msg":
            self._label = cv.create_text(W / 2.0, cy, text=text, fill=_TXT,
                                         font=self._font, anchor="center")

        # Centralizado na horizontal, cfg.overlay_offset_y acima da base do
        # monitor PRIMÁRIO. O offset também é escalado: em 150% ele ficaria
        # colado na barra de tarefas se ficasse em px físicos.
        try:
            off = int(round(float(self.cfg.get("overlay_offset_y", 8)) * s))
        except Exception:
            off = int(round(8 * s))
        x = self._sx + max(0, (self._sw - W) // 2)
        # Ancorada na barra de tarefas (area de trabalho), nao na base da tela.
        y = max(0, _workarea_bottom(self._sy + self._sh) - H - off)
        self._geom = (x, y, W, H)
        self._root.geometry("%dx%d+%d+%d" % (W, H, x, y))
        if self._xwin:
            _x11_shape_capsule(self._xwin, W, H)
        # `wm geometry` do Tk só vira MoveWindow no próximo idle. Como estamos
        # dentro de um callback de `after`, sem forçar aqui o ShowWindow logo
        # abaixo revelaria a pílula no tamanho e na posição antigos por um frame.
        # update_idletasks() (e não update()) não reentra no tick: ele processa
        # só eventos ociosos, nunca timers.
        if size is None:
            # So' no quadro final: nos intermediarios o mainloop ja' aplica a
            # geometria entre dois ticks, e forcar aqui custava o frame inteiro.
            try:
                self._root.update_idletasks()
            except Exception:
                log.debug("overlay: update_idletasks apos geometry falhou", exc_info=True)
            self._lay_key = key
        return target

    def _tk_check_screen(self) -> None:
        """Resolução ou DPI mudaram (dock, projetor, troca de escala)? Refaz."""
        try:
            sx, sy, sw, sh = _primary_rect(self._root.winfo_screenwidth(),
                                           self._root.winfo_screenheight())
            dpi = _dpi_primary(self._hwnd) if _WIN else self._dpi
        except Exception:
            log.debug("overlay: leitura de tela falhou", exc_info=True)
            return
        if (sx, sy, sw, sh) == (self._sx, self._sy, self._sw, self._sh) and dpi == self._dpi:
            return
        log.info("overlay: tela mudou %sx%s+%s+%s@%s -> %sx%s+%s+%s@%s",
                 self._sw, self._sh, self._sx, self._sy, self._dpi, sw, sh, sx, sy, dpi)
        self._sx, self._sy, self._sw, self._sh, self._dpi = sx, sy, sw, sh, dpi
        # _tk_metrics zera o _lay_key, então o próximo tick refaz o desenho e a
        # geometria já com a tela nova.
        self._tk_metrics()

    def _tk_reveal(self) -> None:
        if not _WIN:
            # No X11 o deiconify de uma janela override-redirect so' a mapeia:
            # nao ativa e nao rouba foco (quem dá foco é o KWin, e ele nao
            # gerencia esta janela).
            g = self._geom
            if g:
                x, y, w, h = g
                if self._slide is not None:
                    y = self._slide_y()[0]
                self._root.geometry("%dx%d+%d+%d" % (w, h, x, int(round(y))))
            self._root.deiconify()
            self._root.lift()
            self._vis = True
            return
        hwnd = self._hwnd
        if not hwnd:
            return
        # SW_SHOWNOACTIVATE, nunca deiconify(): deiconify ativa a janela e o
        # ditado seria digitado dentro da pílula.
        _u32.ShowWindow(wt.HWND(hwnd), SW_SHOWNOACTIVATE)
        # Reafirma o topmost sem ativar (outra janela topmost pode ter subido por
        # cima enquanto estávamos escondidos) e, de quebra, a geometria.
        g = self._geom
        if g:
            x, y, w, h = g
            if self._slide is not None:
                y = self._slide_y()[0]         # entra ja' de baixo, nunca "pisca" no lugar
            _u32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), x, int(round(y)), w, h,
                              SWP_NOACTIVATE)
        else:
            _u32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._vis = True

    def _slide_y(self) -> tuple[float, bool]:
        """(y da janela neste quadro do deslize, acabou?). Só com `_slide` ativo."""
        f0, direction = self._slide
        x, y, w, h = self._geom
        t = min(1.0, (time.monotonic() - f0) / _SLIDE_S)
        below = max(0, self._sy + self._sh - y)   # distância até sumir sob a base do primário
        if direction > 0:
            dy = below * (1.0 - t) ** 3           # entrando: rápido, depois freia
        else:
            dy = below * t ** 3                   # saindo: lento, depois acelera
        return y + dy, t >= 1.0

    def _tk_move(self, y: float) -> None:
        if not _WIN:
            if self._geom:
                x, _y, w, h = self._geom
                self._root.geometry("%dx%d+%d+%d" % (w, h, x, int(round(y))))
            return
        hwnd = self._hwnd
        if not hwnd or not self._geom:
            return
        x, _y, w, h = self._geom
        _u32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), x, int(round(y)), w, h,
                          SWP_NOACTIVATE)

    def _tk_tick(self) -> None:
        """Único lugar do processo que toca em Tk. Roda a 33 ms (~30 fps)."""
        st = self._st
        if st["quit"]:
            # Esconde pelo Win32 antes de sair: se o mainloop cair sem passar
            # pelo destroy, a pílula não pode ficar encalhada por cima de tudo.
            self._hide_now()
            try:
                self._root.quit()   # quem destrói é o finally de _tk_main
            except Exception:
                log.exception("overlay: quit() falhou; tentando destroy()")
                try:
                    self._root.destroy()
                except Exception:
                    log.debug("overlay: destroy tambem falhou", exc_info=True)
            return
        try:
            self._frame += 1

            if self._frame % 30 == 0:            # ~0,5 s a 60 fps
                self._tk_check_screen()

            # Leitura única e sem escrita: o tick nunca mexe no dict, senão
            # apagaria um show_recording() que outra thread pediu no mesmo
            # instante e a gravação inteira ficaria sem pílula.
            mode = st["mode"]
            want = bool(st["visible"]) and mode != ""
            if want and mode == "msg":
                until = st["msg_until"]
                if until and time.monotonic() > until:
                    # Mensagem expirou: volta ao traço ocioso, sem escrever no
                    # dict (o tick só lê). Sem traço, some.
                    mode = "idle" if self._idle else ""
                    want = self._idle

            if want:
                text = _LABEL_WORK if mode == "work" else (st["text"] if mode == "msg" else "")
                key = (mode, text, self._scale, self._sw, self._sh)
                if key != self._lay_key:
                    prev = self._geom
                    tgt = self._tk_layout(mode, text)
                    if prev is not None and self._vis and mode != "msg":
                        # Ja' havia uma capsula na tela: em vez de pular de
                        # tamanho, ela escorrega ate' o novo em _ANIM_S.
                        self._anim = (time.monotonic(), prev[2], prev[3], tgt[0], tgt[1])
                        self._tk_layout(mode, text, size=(prev[2], prev[3]))
                    else:
                        self._anim = None
                if self._anim is not None:
                    f0, w0, h0, w1, h1 = self._anim
                    t = min(1.0, (time.monotonic() - f0) / _ANIM_S)
                    e = 1.0 - (1.0 - t) ** 3            # ease-out
                    if t >= 1.0:
                        self._anim = None
                        self._tk_layout(mode, text)
                    else:
                        self._tk_layout(mode, text, size=(round(w0 + (w1 - w0) * e),
                                                          round(h0 + (h1 - h0) * e)))
            else:
                self._anim = None

            if want and not self._vis:
                self._slide = (time.monotonic(), +1)
                self._tk_reveal()
            elif not want and self._vis and (self._slide is None or self._slide[1] > 0):
                self._slide = (time.monotonic(), -1)   # desce antes de sumir

            if self._slide is not None and self._vis:
                y, done = self._slide_y()
                self._tk_move(y)
                if done:
                    entering = self._slide[1] > 0
                    self._slide = None
                    if not entering:
                        self._hide_now()

            if self._vis and self._anim is None and self._slide is None:
                if mode == "rec":
                    self._tk_anim_bars(st)
                elif mode == "work":
                    self._tk_anim_spinner()
        except Exception:
            # Um frame perdido não pode matar o loop: sem mainloop a pílula
            # congela na tela por cima de tudo.
            log.exception("overlay tick falhou")
        # Reagenda pelo PRAZO do proximo quadro, descontando o que este tick
        # gastou. `after(16)` contado do fim do tick dava 16 ms + o custo do
        # quadro, e nas transicoes (geometria + redesenho) isso caia para 20 fps
        # e parecia travado. Se ficou muito para tras, ressincroniza em vez de
        # disparar uma rajada.
        now = time.monotonic()
        self._next_t += _TICK_MS / 1000.0
        if self._next_t < now - 0.1:
            self._next_t = now
        delay = max(1, int(round((self._next_t - now) * 1000)))
        try:
            self._root.after(delay, self._tk_tick)
        except Exception:
            # Sem reagendamento não há mais quem esconda a pílula nem quem leia
            # o "quit": sumir e derrubar o mainloop é melhor que congelar.
            log.exception("overlay nao conseguiu reagendar o tick; encerrando a UI")
            self._hide_now()
            try:
                self._root.quit()
            except Exception:
                log.debug("overlay: quit() apos falha de after() tambem falhou", exc_info=True)

    def _tk_anim_bars(self, st) -> None:
        tgt = _norm_level(st["level"])
        cur = self._lvl
        # ataque rápido, decaimento lento: o RMS cru pisca feito ruído
        a = 0.45 if tgt > cur else 0.10
        cur += (tgt - cur) * a
        self._lvl = cur

        cv, cy = self._cv, self._h / 2.0
        span = self._bar_max - self._bar_min
        for i, item in enumerate(self._bars):
            osc = 0.55 + 0.45 * math.sin(time.monotonic() * 8.0 + i * 0.85)
            want = self._bar_min + span * cur * self._bar_shape[i] * osc
            self._bar_a[i] += (want - self._bar_a[i]) * 0.5
            h = self._bar_a[i]
            x = self._bar_x[i]
            cv.coords(item, x, cy - h, x, cy + h)

    def _tk_anim_spinner(self) -> None:
        if self._arc is None:
            return
        now = time.monotonic()
        start = (-now * 270.0) % 360
        extent = 70 + 60 * (0.5 + 0.5 * math.sin(now * 3.3))
        self._cv.itemconfigure(self._arc, start=start, extent=extent)


# --------------------------------------------------------------------------
# Sons de feedback
# --------------------------------------------------------------------------
_SND_SR = 44100
_WAV_CACHE: dict[str, bytes] = {}   # segura os bytes vivos: SND_ASYNC|SND_MEMORY
                                    # toca DEPOIS do return, lendo o buffer que
                                    # passamos. Se ele for coletado, o processo cai.
_SND_Q: "queue.Queue[str] | None" = None
_SND_LOCK = threading.Lock()
_SND_CFG = None


def _sounds_enabled(cfg=None) -> bool:
    global _SND_CFG
    if cfg is None:
        with _SND_LOCK:
            if _SND_CFG is None:
                _SND_CFG = _load_cfg()
            cfg = _SND_CFG
    try:
        return bool(cfg.get("sounds", True))
    except Exception:
        return True


def _blip(freqs, ms: int, vol: float = 0.22) -> bytes:
    """WAV PCM 16 bits mono em memória: varredura linear entre `freqs` com
    envelope de ataque/decaimento — sem envelope, o clique do corte abrupto
    fica mais audível que o próprio bipe."""
    n = max(1, int(_SND_SR * ms / 1000.0))
    atk = max(1, int(_SND_SR * 0.006))
    rel = max(1, int(_SND_SR * 0.045))
    buf = array.array("h", bytes(2 * n))
    ph = 0.0
    segs = len(freqs) - 1
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        p = t * segs
        k = min(segs - 1, int(p)) if segs > 0 else 0
        f = freqs[k] + (freqs[k + 1] - freqs[k]) * (p - k) if segs > 0 else freqs[0]
        ph += 2.0 * math.pi * f / _SND_SR
        if i < atk:
            env = i / atk
        elif i > n - rel:
            env = max(0.0, (n - i) / rel)
        else:
            env = 1.0
        s = math.sin(ph) * env * vol
        buf[i] = int(max(-1.0, min(1.0, s)) * 32767)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SND_SR)
        w.writeframes(buf.tobytes())
    return bio.getvalue()


def _wav_for(kind: str) -> bytes:
    data = _WAV_CACHE.get(kind)
    if data is None:
        data = _blip((760.0, 1180.0), 95) if kind == "start" else _blip((1180.0, 700.0), 110)
        _WAV_CACHE[kind] = data
    return data


def _snd_worker() -> None:
    """Thread daemon dedicada aos bipes. Nunca morre por causa de um som."""
    q = _SND_Q
    while True:
        try:
            kind = q.get()
        except Exception:
            log.debug("sound queue morreu; thread de som saindo", exc_info=True)
            return
        try:
            winsound.PlaySound(_wav_for(kind),
                               winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            try:
                winsound.Beep(880 if kind == "start" else 660, 90)
            except Exception:
                log.debug("ping %s falhou (sem dispositivo de saida?)", kind, exc_info=True)


def _ping(kind: str, cfg=None) -> None:
    """Enfileira o som numa thread dedicada.

    PlaySound com SND_ASYNC já volta na hora, mas abrir um endpoint de saída
    travado pode segurar centenas de ms — e o caller aqui é a thread worker,
    no caminho quente do ditado. A fila desacopla e descarta se encher:
    feedback sonoro atrasado não vale segurar o texto do usuário.
    """
    if not _sounds_enabled(cfg):
        return
    global _SND_Q
    try:
        with _SND_LOCK:
            if _SND_Q is None:
                _SND_Q = queue.Queue(maxsize=4)
                try:
                    threading.Thread(target=_snd_worker, name="sound", daemon=True).start()
                except Exception:
                    # Sem worker a fila encheria e engoliria todo som para sempre.
                    _SND_Q = None
                    log.debug("nao foi possivel subir a thread de som", exc_info=True)
                    return
            q = _SND_Q
        q.put_nowait(kind)
    except queue.Full:
        pass
    except Exception:
        log.debug("nao foi possivel enfileirar o som %s", kind, exc_info=True)


def ping_start(cfg=None) -> None:
    """Bipe ascendente: começou a gravar. Nunca bloqueia o caller."""
    _ping("start", cfg)


def ping_stop(cfg=None) -> None:
    """Bipe descendente: parou de gravar. Nunca bloqueia o caller."""
    _ping("stop", cfg)
