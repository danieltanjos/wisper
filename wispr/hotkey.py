# -*- coding: utf-8 -*-
"""Hotkey global do wisper: `Win+A` começa a gravar, `Enter` finaliza, `Esc` cancela.

Por que um hook de baixo nível e não `RegisterHotKey`: nesta máquina
`RegisterHotKey(MOD_WIN, 'A')` devolve 0 com erro 1409 — o shell do Windows 11 é
dono de todos os `Win+letra` e nem deixa registrar. Único caminho é
`WH_KEYBOARD_LL` devolvendo 1 para engolir o evento.
Ver docs/ARCHITECTURE.md seção 3.

Três threads saem daqui:
  - hook    : instala o `WH_KEYBOARD_LL` e bombeia `GetMessageW` (o hook precisa de
              uma thread com fila de mensagens, senão o Windows o despeja);
  - worker  : tira eventos da fila e chama os callbacks do app;
  - watchdog: detecta hook despejado em silêncio e manda reinstalar.

O hook proc em si só seta flags e faz `queue.put()`. Orçamento medido: 0,017 ms
de custo médio contra 300 ms de `LowLevelHooksTimeout` default.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import queue
import threading
import time

from wispr import config

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)

# --------------------------------------------------------------------------- #
# constantes Win32
# --------------------------------------------------------------------------- #
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_QUIT = 0x0012
WM_WISPR_REINSTALL = 0x0401          # WM_USER+1; mensagem de thread, nunca despachada
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

VK_MASK = 0xE8                       # VK "não atribuído": a tecla-máscara mais segura
VK_LCONTROL = 0xA2
VK_RMENU = 0xA5                      # AltGr chega como LCtrl sintético + este

# Tags em dwExtraInfo. O hook ignora tudo que carregue uma delas, senão o app se
# retriggera ao digitar a própria transcrição. 0x57495350 é o mesmo valor de
# wispr.inject.INJECT_TAG — duplicado aqui de propósito para não criar ciclo de
# import entre hotkey.py e inject.py (start() confere os dois em tempo de execução).
MASK_TAG = 0x57495352
INJECT_TAG = 0x57495350
IGNORED_TAGS = frozenset({MASK_TAG, INJECT_TAG})

# watchdog
WATCHDOG_PERIOD_S = 3.0
WATCHDOG_MAX_PERIOD_S = 60.0         # teto do backoff quando a instalação falha em série
INPUT_RECENT_MS = 1500               # o SO viu input há menos disso...
HOOK_SILENT_S = 5.0                  # ...mas nosso hook proc não dispara há mais disso
HOOK_FAIL_ALERT = 3                  # falhas seguidas até avisar o app (uma vez por série)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)

u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HMODULE, w.DWORD]
u32.SetWindowsHookExW.restype = w.HHOOK
u32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
u32.UnhookWindowsHookEx.restype = w.BOOL
u32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
u32.CallNextHookEx.restype = ctypes.c_ssize_t
u32.GetMessageW.argtypes = [ctypes.c_void_p, w.HWND, w.UINT, w.UINT]
u32.GetMessageW.restype = ctypes.c_int
u32.TranslateMessage.argtypes = [ctypes.c_void_p]
u32.TranslateMessage.restype = w.BOOL
u32.DispatchMessageW.argtypes = [ctypes.c_void_p]
u32.DispatchMessageW.restype = ctypes.c_ssize_t   # LRESULT: com o default c_int o
                                                  # retorno de 64 bits truncaria
u32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
u32.PostThreadMessageW.restype = w.BOOL
u32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = w.UINT
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
u32.GetLastInputInfo.restype = w.BOOL

k32.GetModuleHandleW.argtypes = [w.LPCWSTR]
k32.GetModuleHandleW.restype = w.HMODULE   # OBRIGATÓRIO: sem isto o handle de 64 bits
                                           # trunca e SetWindowsHookExW falha com erro 126
k32.GetCurrentThreadId.argtypes = []
k32.GetCurrentThreadId.restype = w.DWORD
k32.GetTickCount.argtypes = []
k32.GetTickCount.restype = w.DWORD
k32.GetCurrentProcess.argtypes = []
k32.GetCurrentProcess.restype = w.HANDLE
k32.CloseHandle.argtypes = [w.HANDLE]
k32.CloseHandle.restype = w.BOOL

adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
adv.OpenProcessToken.restype = w.BOOL
adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                    w.DWORD, ctypes.POINTER(w.DWORD)]
adv.GetTokenInformation.restype = w.BOOL


def _get_logger() -> logging.Logger:
    """Quem configura os handlers é wispr.logging_setup; se ele ainda não estiver
    disponível (import isolado deste módulo), cai num logger cru em vez de explodir."""
    try:
        from wispr import logging_setup
        return logging_setup.get(__name__)
    except Exception:
        return logging.getLogger(__name__)


log = _get_logger()


# --------------------------------------------------------------------------- #
# tecla-máscara
# --------------------------------------------------------------------------- #
_INPUT_SIZE = ctypes.sizeof(INPUT)      # 40 em x64, ver docs/ARCHITECTURE.md seção 5


def _build_mask_inputs() -> ctypes.Array:
    arr = (INPUT * 2)()
    for i, flags in enumerate((0, KEYEVENTF_KEYUP)):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(wVk=VK_MASK, wScan=0, dwFlags=flags, time=0,
                                 dwExtraInfo=MASK_TAG)
    return arr


# Montado uma única vez no import: quem chama _tap_mask() é o hook proc, e alocar
# structs no caminho quente é justamente o tipo de trabalho que estoura o orçamento.
_MASK_INPUTS = _build_mask_inputs()


def _tap_mask() -> None:
    """Toque down+up em VK_MASK, marcado com MASK_TAG para nosso hook ignorar.

    Sem essa máscara o painel "Pesquisar" (`Windows.UI.Core.CoreWindow`) aparece no
    keyup do Win, mesmo engolindo o A inteiro. Ver docs/ARCHITECTURE.md seção 3."""
    u32.SendInput(2, _MASK_INPUTS, _INPUT_SIZE)


# --------------------------------------------------------------------------- #
# parsing do chord
# --------------------------------------------------------------------------- #
# Cada modificador lista o VK genérico e os dois laterais: o hook de baixo nível
# entrega sempre o lateral (VK_LMENU etc.), mas o genérico aparece em input
# injetado por outros programas.
_MOD_VKS = {
    "win": (0x5B, 0x5C),                    # VK_LWIN, VK_RWIN
    "ctrl": (0x11, 0xA2, 0xA3),             # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
    "alt": (0x12, 0xA4, 0xA5),              # VK_MENU, VK_LMENU, VK_RMENU
    "shift": (0x10, 0xA0, 0xA1),            # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
}
_MOD_ALIASES = {
    "win": "win", "super": "win", "meta": "win", "cmd": "win", "windows": "win",
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "menu": "alt", "altgr": "alt",
    "shift": "shift", "shft": "shift",
}
_KEY_VKS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20, "tab": 0x09, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "capslock": 0x14, "pause": 0x13,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "scrolllock": 0x91, "printscreen": 0x2C, "apps": 0x5D,
}
_KEY_VKS.update({"f%d" % i: 0x6F + i for i in range(1, 25)})   # F1..F24 = 0x70..0x87


def _key_vk(name: str) -> int | None:
    """Nome de tecla -> virtual-key code. Devolve None se não souber."""
    n = (name or "").strip().lower()
    if not n:
        return None
    if len(n) == 1 and ("a" <= n <= "z" or "0" <= n <= "9"):
        return ord(n.upper())
    return _KEY_VKS.get(n)


def parse_chord(spec: str) -> tuple[frozenset[str], int]:
    """'win+a' -> (frozenset({'win'}), 0x41). Levanta ValueError no que não der."""
    parts = [p for p in (spec or "").strip().lower().replace("-", "+").split("+") if p]
    if len(parts) < 2:
        raise ValueError("chord precisa de ao menos um modificador e uma tecla: %r" % spec)
    mods = set()
    for p in parts[:-1]:
        m = _MOD_ALIASES.get(p)
        if m is None:
            raise ValueError("modificador desconhecido em %r: %r" % (spec, p))
        mods.add(m)
    vk = _key_vk(parts[-1])
    if vk is None:
        raise ValueError("tecla desconhecida em %r: %r" % (spec, parts[-1]))
    return frozenset(mods), vk


def is_elevated() -> bool:
    """True se este processo roda elevado.

    Importa saber: com uma janela **elevada** em foco, um hook de integridade média
    recebe ZERO eventos (UIPI) e o `Win+A` vaza para a Central de Ações. Não tem
    conserto sem rodar elevado — mas dá para o app explicar por que emudeceu.
    Ver docs/ARCHITECTURE.md seção 3."""
    TOKEN_QUERY = 0x0008
    TOKEN_ELEVATION = 20        # TOKEN_INFORMATION_CLASS::TokenElevation
    token = w.HANDLE()
    try:
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            elevated = w.DWORD(0)
            got = w.DWORD(0)
            ok = adv.GetTokenInformation(token, TOKEN_ELEVATION, ctypes.byref(elevated),
                                         ctypes.sizeof(elevated), ctypes.byref(got))
            return bool(ok) and bool(elevated.value)
        finally:
            k32.CloseHandle(token)
    except Exception:
        log.exception("is_elevated() failed")
        return False


def _idle_ms() -> int:
    """Há quanto tempo o SO não vê input nenhum (teclado ou mouse)."""
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not u32.GetLastInputInfo(ctypes.byref(li)):
        # Falhou: fingir "ocioso há muito tempo". Devolver 0 significaria "o usuário
        # acabou de digitar" e faria o watchdog reinstalar o hook a cada 3 s de graça.
        return 0xFFFFFFFF
    # GetTickCount e dwTime são ambos DWORD e viram a volta em 49,7 dias:
    # a máscara mantém a diferença correta na virada.
    return (k32.GetTickCount() - li.dwTime) & 0xFFFFFFFF


class HotkeyEngine:
    """Hook global de teclado com chord configurável.

    Callbacks (`on_start`, `on_stop`, `on_cancel`, `on_error`) são chamados na
    thread worker interna, nunca dentro do hook proc.

    Duas flags públicas, com donos diferentes — é aqui que mora a diferença entre
    "Esc cancela" e "Esc vaza para o Discord":

    `recording`
        Quem escreve é **o app**, e só ele (`App.on_start` confirma dentro do
        próprio lock; `on_stop`/`on_cancel`/falhas desligam). O engine nunca liga
        essa flag: entre o Win+A e o microfone aberto existem 80–92 ms medidos
        (ARCHITECTURE.md seção 4), e ligar antes faria o hook engolir o Enter do
        usuário para uma gravação que ainda não existe. Ela governa **só** o Enter.

    `cancellable`
        O engine liga no instante em que entrega o "start" ao app e **nunca
        desliga sozinho** — nem no Enter, nem no Esc. Quem desliga é o
        `wispr/app.py`: no `finally` do job de processamento e no caminho de
        falha. É ela que governa o Esc, e é por isso que ela tem que sobreviver ao
        `recording = False` do Enter: o ditado só termina quando o texto foi
        injetado (ou descartado), e até lá o CONTRACT.md promete que "Esc volta
        para idle sem injetar nada".

    Saúde do hook, para o app e a bandeja lerem ao vivo: `alive`, `installs`
    (sucessos, monotônico) e `failed_installs` (falhas acumuladas)."""

    def __init__(self, on_start, on_stop, on_cancel, cfg=None, *, on_error=None):
        self.cfg = cfg or config.load()
        self.recording = False          # dono: o app (ver docstring da classe)
        self.cancellable = False        # dono da limpeza: o app (ver docstring da classe)
        self.installs = 0               # instalações bem-sucedidas
        self.failed_installs = 0        # SetWindowsHookExW que voltou 0

        self.q: queue.Queue = queue.Queue()
        self._cbs = {"start": on_start, "stop": on_stop, "cancel": on_cancel}
        self._on_error = on_error       # avisa que o hook parou de instalar

        spec = str(self.cfg.get("hotkey", config.DEFAULTS["hotkey"]))
        try:
            self._mods, self._vk = parse_chord(spec)
        except ValueError as exc:
            log.error("invalid hotkey %r (%s); falling back to %r",
                      spec, exc, config.DEFAULTS["hotkey"])
            spec = config.DEFAULTS["hotkey"]
            self._mods, self._vk = parse_chord(spec)
        self.chord = spec

        # A máscara só existe por causa da tecla Win: sem ela o painel "Pesquisar"
        # (Windows.UI.Core.CoreWindow) aparece no keyup do Win. Ctrl+Alt+A não
        # precisa de máscara nem de rastreio do Win.
        self._needs_mask = "win" in self._mods
        self._mod_vks = {name: _MOD_VKS[name] for name in self._mods}
        self._vk_to_mod = {vk: name for name, vks in self._mod_vks.items() for vk in vks}
        self._down = {name: False for name in self._mods}
        self._vk_down: set[int] = set()  # VKs de modificador fisicamente baixos
        self._chord = False             # chord já disparou e ainda não soltou a tecla
        self._eat_up = set()            # VKs cujo keydown engolimos: engolir o keyup também
        # Só `ctrl+alt+...` colide com AltGr; para `win+a` isso é um booleano morto.
        self._altgr_guard = {"ctrl", "alt"} <= set(self._mods)
        self._altgr_t: int | None = None   # tick do LCtrl que pode ser AltGr

        self._stop_vk = self._resolve_key("stop_key", "enter", 0x0D)
        self._cancel_vk = self._resolve_key("cancel_key", "esc", 0x1B)

        self._hook = None
        self._tid = 0
        self._hook_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._last_fire = time.monotonic()
        self._proc_errors = 0           # só o hook proc escreve; só o watchdog lê
        self._proc_errors_logged = 0
        self._consec_fails = 0          # falhas de instalação em série (zera no sucesso)
        self._error_sent = False        # on_error já disparou para ESTA série
        self._last_error = ""
        self._pending = False           # um "start" entregue ao app e ainda em execução
        self._ready = threading.Event()
        self._stopping = threading.Event()
        # Um único lock, pequeno: serializa nascimento da thread do hook contra o
        # WM_QUIT do stop(). Sem ele o watchdog ressuscita o hook exatamente entre a
        # leitura do tid e o post, e sobra um WH_KEYBOARD_LL vivo sem dono.
        self._lk = threading.Lock()
        self._started = False
        self._proc = HOOKPROC(self._hookproc)   # referência forte: sem ela o Python
                                                # coleta o callback e o processo cai duro

    def _resolve_key(self, key: str, default_name: str, default_vk: int) -> int:
        """Lê `stop_key`/`cancel_key` da config, degradando com log em vez de
        silenciosamente virar outra tecla."""
        raw = str(self.cfg.get(key, default_name))
        vk = _key_vk(raw)
        if vk is None:
            log.error("invalid %s %r; falling back to %r", key, raw, default_name)
            return default_vk
        return vk

    # ------------------------------------------------------------------ #
    # hook proc — território de microssegundos
    # ------------------------------------------------------------------ #
    def _hookproc(self, nCode, wParam, lParam):
        # NUNCA logar, travar lock, tocar áudio ou fazer I/O aqui: o orçamento é de
        # 300 ms e qualquer estouro faz o Windows despejar o hook em silêncio.
        # O try/except é grátis em 3.11 enquanto não dispara, e sem ele uma exceção
        # viraria traceback no stderr que o pythonw.exe não tem — invisível, com o
        # ctypes devolvendo 0 e a tecla vazando para a janela em foco.
        try:
            if nCode == HC_ACTION:
                self._last_fire = time.monotonic()      # sinal de vida para o watchdog
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                # Tecla com uma das nossas tags é tecla que NÓS injetamos (máscara ou
                # transcrição): passa direto, senão o app se retriggera digitando.
                if kb.dwExtraInfo not in IGNORED_TAGS:
                    vk = kb.vkCode
                    down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    # AltGr do ABNT2 não é uma tecla: o Windows sintetiza LCtrl e, no
                    # MESMO tick, RMenu. Guardar o tick do LCtrl anterior (e só dele,
                    # qualquer outro evento no meio quebra a cadeia) é o que permite
                    # descartá-lo lá embaixo.
                    prev_lctrl = None
                    if self._altgr_guard:
                        prev_lctrl = self._altgr_t
                        self._altgr_t = kb.time if (down and vk == VK_LCONTROL) else None
                    mod = self._vk_to_mod.get(vk)
                    if mod is not None:
                        # Rastreio por VK e não por nome: com as duas laterais baixas,
                        # soltar só uma não pode zerar o modificador inteiro.
                        if down:
                            if (vk == VK_RMENU and prev_lctrl is not None
                                    and kb.time == prev_lctrl):
                                # Ninguém acerta LCtrl e RAlt dentro do mesmo tick de
                                # GetTickCount (~15,6 ms) com os dedos: isto é AltGr.
                                # Fingir o Ctrl solto faz `ctrl+alt+a` não casar, e o A
                                # do usuário chega inteiro ao app em vez de virar
                                # gravação. Heurística, não garantia: ver start().
                                self._vk_down.discard(VK_LCONTROL)
                                self._down["ctrl"] = not self._vk_down.isdisjoint(
                                    self._mod_vks["ctrl"])
                            self._vk_down.add(vk)
                        else:
                            self._vk_down.discard(vk)
                        self._down[mod] = not self._vk_down.isdisjoint(self._mod_vks[mod])
                    elif vk == self._vk:
                        if down:
                            if all(self._down.values()):
                                if not self._chord:     # auto-repeat não redispara
                                    self._chord = True
                                    if self._needs_mask:
                                        _tap_mask()     # desarma o menu Iniciar no keyup do Win
                                    self.q.put("start")
                                return 1                # engole o keydown...
                        elif self._chord:
                            self._chord = False
                            return 1                    # ...E o keyup (medido: sem isso a
                                                        # Central de Ações abre)
                    elif self.recording and vk == self._stop_vk:
                        # Enter só é engolido depois que o app CONFIRMOU a gravação:
                        # engolir antes é o bug do "wisper comeu meu Enter" no Discord.
                        if down:
                            self._eat_up.add(vk)
                            self.q.put("stop")
                            return 1
                        if vk in self._eat_up:
                            self._eat_up.discard(vk)
                            return 1
                    elif self.cancellable and vk == self._cancel_vk:
                        # Esc, ao contrário, vale a gravação INTEIRA — inclusive os
                        # ~0,5 s de transcrição depois do Enter, quando `recording` já
                        # é False. Gatear no `recording` é o que fazia o Esc fechar o
                        # diálogo do app em foco e ainda assim digitar a transcrição.
                        if down:
                            self._eat_up.add(vk)
                            self.q.put("cancel")
                            return 1
                        if vk in self._eat_up:
                            self._eat_up.discard(vk)
                            return 1
                    elif not down and vk in self._eat_up:
                        # keyup de uma tecla engolida depois que a gravação já terminou
                        self._eat_up.discard(vk)
                        return 1
        except Exception:
            # Contabiliza e segue: quem loga isso é o watchdog, fora do caminho quente.
            self._proc_errors += 1
        return u32.CallNextHookEx(None, nCode, wParam, lParam)

    # ------------------------------------------------------------------ #
    # threads
    # ------------------------------------------------------------------ #
    def _fire(self, name: str) -> None:
        try:
            self._cbs[name]()
        except Exception:
            # Sob pythonw.exe não existe console: exceção não logada é invisível.
            log.exception("hotkey callback %r raised", name)

    def _fire_error(self, reason: str) -> None:
        cb = self._on_error
        if cb is None:
            return
        try:
            cb(reason)
        except Exception:
            log.exception("hotkey on_error callback raised")

    def _worker(self) -> None:
        while True:
            try:
                ev = self.q.get()
            except Exception:
                log.exception("hotkey queue failed")
                return
            if ev is None:
                return
            if type(ev) is tuple:
                # ("error", motivo): saúde do hook, vinda da thread do hook. Passa
                # por aqui só para o callback sair na mesma thread dos outros.
                self._fire_error(ev[1])
                continue
            if ev == "start":
                # Nem `recording` nem `cancellable` são desligados aqui: o dono das
                # duas é o app (ver docstring da classe). `_pending` existe porque
                # `recording` só fica True lá na frente, depois do microfone abrir —
                # e um stop() seguido de start() pode deixar por alguns instantes um
                # worker antigo ainda preso dentro de on_start ao lado do novo.
                if self.recording or self._pending:
                    continue                    # duplo toque colapsa num início só
                self._pending = True
                self.cancellable = True         # Esc já cancela, antes mesmo do mic
                try:
                    self._fire("start")
                finally:
                    self._pending = False
            elif ev == "stop" and self.recording:
                self._fire("stop")
            elif ev == "cancel" and self.cancellable:
                self._fire("cancel")

    def _reset_state(self) -> None:
        """Zera o estado de teclas. Modificador preso é real: se engolimos o A com o
        Win baixo e o keyup se perdeu (hook despejado, janela elevada em foco), o
        flag ficaria True e todo 'a' viraria gravação."""
        for name in self._down:
            self._down[name] = False
        self._vk_down.clear()
        self._chord = False
        self._eat_up.clear()
        self._altgr_t = None

    def _install(self) -> bool:
        """Só pode rodar na thread do hook: quem instala é quem bombeia as mensagens.

        Nunca levanta. Conta a falha em `failed_installs`, e na terceira seguida
        avisa o app uma única vez por série — depois de lock/unlock ou de desktop
        heap esgotado isto aqui falha para sempre, e sem esse aviso todo sinal
        visível continua dizendo que está tudo bem."""
        handle, detail = 0, ""
        try:
            handle = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                           k32.GetModuleHandleW(None), 0)
            if not handle:
                detail = str(ctypes.WinError(ctypes.get_last_error()))
        except Exception as exc:
            # ctypes não costuma levantar aqui; se levantar, virar "falha de
            # instalação" é melhor do que derrubar a thread do hook inteira.
            handle, detail = 0, "%s: %s" % (type(exc).__name__, exc)
        if not handle:
            self._hook = None
            self.failed_installs += 1
            self._consec_fails += 1
            self._last_error = detail
            # Sem rate limit isto sozinho enche o log rotativo de 1 MB e apaga o
            # diagnóstico do que quebrou antes.
            if self._consec_fails == 1 or self._consec_fails % 10 == 0:
                log.error("SetWindowsHookExW failed (%d in a row, %d total): %s",
                          self._consec_fails, self.failed_installs, self._last_error)
            if self._consec_fails >= HOOK_FAIL_ALERT and not self._error_sent:
                self._error_sent = True
                try:
                    self.q.put(("error",
                                "O atalho %s parou de funcionar: o Windows recusou "
                                "instalar o hook de teclado %d vezes seguidas (%s)."
                                % (self.chord, self._consec_fails, self._last_error)))
                except Exception:
                    log.exception("could not queue the hook failure notice")
            return False
        self._hook = handle
        self.installs += 1
        if self._consec_fails:
            log.info("keyboard hook recovered after %d failed install(s)",
                     self._consec_fails)
        self._consec_fails = 0
        self._error_sent = False        # uma nova série pode avisar de novo
        self._last_error = ""
        self._last_fire = time.monotonic()
        self._reset_state()
        log.info("keyboard hook installed (handle=0x%X, install #%d, chord=%s)",
                 handle, self.installs, self.chord)
        return True

    def _hookthread(self) -> None:
        my_tid = k32.GetCurrentThreadId()
        try:
            self._tid = my_tid
            self._install()
            self._ready.set()
            msg = w.MSG()
            while True:
                r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == -1:
                    log.error("GetMessageW failed: %s", ctypes.WinError(ctypes.get_last_error()))
                    break
                if r == 0:                              # WM_QUIT
                    break
                if msg.message == WM_WISPR_REINSTALL:
                    if self._hook:
                        u32.UnhookWindowsHookEx(self._hook)
                        self._hook = None
                    self._install()
                    continue                            # mensagem de thread: não despachar
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("hook thread died")
        finally:
            self._ready.set()
            if self._hook:
                try:
                    u32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    log.exception("UnhookWindowsHookEx failed")
                self._hook = None
            # Só apaga o tid se ainda for o nosso: se o watchdog já ressuscitou a
            # thread, zerar aqui deixaria o watchdog sem para onde postar.
            if self._tid == my_tid:
                self._tid = 0
            log.info("hook thread exiting (installs=%d)", self.installs)

    def _spawn_hook_thread(self, stopping: threading.Event | None = None) -> bool:
        """Sobe (ou ressuscita) a thread do hook. Devolve False se já estamos parando.

        Segura `self._lk` e reconfere o flag de parada lá dentro porque `stop()`
        lê o tid e posta o WM_QUIT sob o mesmo lock: sem isso o watchdog pode
        ressuscitar a thread entre as duas coisas, o WM_QUIT vai para um tid morto
        e a thread nova fica parada no `GetMessageW` com um `WH_KEYBOARD_LL` vivo
        que ninguém vai desinstalar — `Win+A` deixa de gravar E de abrir a Central
        de Ações até o processo morrer de verdade."""
        with self._lk:
            if stopping is not None and stopping.is_set():
                return False
            self._ready.clear()
            t = threading.Thread(target=self._hookthread, name="hook", daemon=True)
            self._hook_thread = t
            t.start()
            return True

    def _sync_modifiers(self) -> None:
        """Compara nossos flags com o estado físico das teclas e destrava o que
        ficou preso (ver _reset_state)."""
        for name, vks in self._mod_vks.items():
            if not self._down.get(name):
                continue
            if not any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
                for vk in vks:
                    self._vk_down.discard(vk)
                self._down[name] = False
                # Só desarma o chord se a própria tecla do chord também estiver solta:
                # com ela ainda baixa, limpar o flag faria o keyup dela vazar.
                if not (u32.GetAsyncKeyState(self._vk) & 0x8000):
                    self._chord = False
                log.warning("stuck modifier %r cleared (physically up)", name)

    def _report_proc_errors(self) -> None:
        """O hook proc não pode logar; ele só conta. O relato sai aqui, senão uma
        exceção lá dentro seria absolutamente invisível sob pythonw.exe."""
        n = self._proc_errors
        if n != self._proc_errors_logged:
            self._proc_errors_logged = n
            log.error("hook proc raised %d time(s) so far; keys may have leaked to the "
                      "focused window", n)

    def _watchdog_period(self) -> float:
        """Backoff enquanto a instalação falha em série.

        Com período fixo o watchdog martela `SetWindowsHookExW` a cada 3 s para
        sempre depois de um lock/unlock ou de uma troca rápida de usuário. Isso não
        conserta nada, e o que se perde no atraso (destravar modificador preso,
        relatar erro do hook proc) só existe quando existe hook — que é justamente
        o caso em que o backoff não entra."""
        n = self._consec_fails
        if n <= 0:
            return WATCHDOG_PERIOD_S
        return min(WATCHDOG_MAX_PERIOD_S, WATCHDOG_PERIOD_S * (2.0 ** min(n, 6)))

    def _post_reinstall(self, why: str) -> bool:
        """Pede reinstalação à thread do hook (só ela pode instalar)."""
        with self._lk:
            tid = self._tid
        if not tid:
            return False
        if not u32.PostThreadMessageW(tid, WM_WISPR_REINSTALL, 0, 0):
            log.error("PostThreadMessageW(reinstall: %s) failed: %s", why,
                      ctypes.WinError(ctypes.get_last_error()))
            return False
        return True

    def _watchdog(self, stopping: threading.Event) -> None:
        """Se o SO viu input há pouco (`GetLastInputInfo`) mas nosso hook proc não
        dispara há mais de 5 s, o Windows despejou o hook em silêncio: reinstalar.

        Recebe o próprio Event por parâmetro para que um watchdog de um ciclo
        anterior (stop() seguido de start()) morra em vez de virar um clone."""
        while not stopping.wait(self._watchdog_period()):
            try:
                if stopping.is_set():
                    return
                self._report_proc_errors()
                self._sync_modifiers()
                t = self._hook_thread
                if t is None or not t.is_alive():
                    # GetMessageW devolveu -1, ou a thread morreu: sem ressuscitá-la o
                    # hotkey fica morto para sempre e ninguém fica sabendo.
                    log.error("hook thread is gone (installs=%d); restarting it",
                              self.installs)
                    if not self._spawn_hook_thread(stopping):
                        return
                    self._ready.wait(2.0)
                    continue
                if not self._hook:
                    # Instalação falhando: não adianta esperar a heurística de silêncio,
                    # já sabemos que não há hook. Quem segura a frequência é o backoff.
                    self._post_reinstall("hook is not installed")
                    continue
                silent = time.monotonic() - self._last_fire
                if _idle_ms() < INPUT_RECENT_MS and silent > HOOK_SILENT_S:
                    log.warning("hook silent for %.1fs while the OS saw input -> reinstalling",
                                silent)
                    self._last_fire = time.monotonic()   # evita repetir antes da reinstalação
                    self._post_reinstall("silent for %.1fs" % silent)
            except Exception:
                log.exception("hotkey watchdog iteration failed")

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        """True enquanto existe um `WH_KEYBOARD_LL` nosso instalado.

        Limite honesto: quando o Windows despeja o hook em silêncio — o caso que o
        watchdog caça — ninguém nos avisa, então isto continua True até a
        reinstalação. Para "está piorando?", compare `failed_installs` entre duas
        leituras; ele cresce a cada recusa e `alive` cai junto."""
        return bool(self._hook)

    def start(self) -> "HotkeyEngine":
        """Sobe worker, hook e watchdog. Nunca levanta: se o hook não instalar, o
        watchdog continua tentando e o app fica sabendo por `installs == 0`."""
        if self._started:
            return self
        self._started = True
        # Event novo a cada ciclo: threads de um start() anterior continuam vendo o
        # Event antigo (já setado) e morrem sozinhas em vez de duplicar.
        self._stopping = threading.Event()
        stopping = self._stopping
        self._ready.clear()
        self._check_inject_tag()
        if self._stop_vk == self._vk or self._cancel_vk == self._vk:
            log.error("stop/cancel key collides with the chord key (vk 0x%02X): the "
                      "chord branch wins and the collided key will never fire", self._vk)
        if self._altgr_guard:
            # Esta máquina tem ABNT2 instalado, e nele AltGr é LCtrl sintético + RAlt:
            # sem filtro, AltGr+A vira gravação e o A some. O filtro do hook proc é uma
            # heurística de mesmo tick (o Windows não marca o LCtrl sintético de forma
            # confiável), então quem digita AltGr o dia inteiro deve preferir 'win+a'.
            log.warning("chord %r shares its modifiers with AltGr (ABNT2/US-Intl): "
                        "AltGr presses are filtered by a same-tick heuristic, not by a "
                        "flag from Windows; prefer 'win+a' if you type AltGr often",
                        self.chord)
        elevated = is_elevated()
        log.info("hotkey engine starting: chord=%r mask=%s stop_vk=0x%02X cancel_vk=0x%02X "
                 "elevated=%s", self.chord, self._needs_mask, self._stop_vk,
                 self._cancel_vk, elevated)
        if not elevated:
            # Diagnóstico, não conserto: sob janela elevada em foco o hook fica mudo.
            log.info("running unelevated: the hook receives no events while an elevated "
                     "window has focus (UIPI)")
        self._worker_thread = threading.Thread(target=self._worker, name="hk-worker",
                                               daemon=True)
        self._worker_thread.start()
        self._spawn_hook_thread(stopping)
        threading.Thread(target=self._watchdog, args=(stopping,), name="hk-watchdog",
                         daemon=True).start()
        self._ready.wait(2.0)
        if not self._hook:
            log.error("keyboard hook is NOT installed; %r will not work until the watchdog "
                      "reinstalls it", self.chord)
        return self

    def stop(self) -> None:
        """Desliga tudo. Idempotente e à prova de erro: é chamada no shutdown.

        **BLOQUEIA, pior caso ~4 s** (2 s de join na thread do hook + 2 s na do
        worker), e ~6 s no caso raro de `stop()` logo depois de `start()`, quando
        ainda esperamos a thread do hook publicar o tid dela. Quem chama do
        caminho de desligamento da bandeja precisa contar com isso.

        Espera a thread do hook morrer de verdade porque `stop()` também serve a um
        toggle enable/disable: dois hooks vivos ao mesmo tempo engoliriam a tecla
        duas vezes e o segundo nunca seria desinstalado.

        Não mexe em `cancellable`: o dono dela é o app, e um ditado em processamento
        continua cancelável mesmo com o engine descendo."""
        self._stopping.set()
        self._started = False
        # Tudo que envolve tid/nascimento de thread vai sob o mesmo lock do
        # _spawn_hook_thread(), senão o watchdog ressuscita o hook no meio e o
        # WM_QUIT vai para um tid que já morreu (ver _spawn_hook_thread).
        with self._lk:
            hook_t, worker_t = self._hook_thread, self._worker_thread
            if hook_t is not None and hook_t.is_alive() and not self._tid:
                # start() seguido de stop() imediato: a thread do hook ainda não
                # publicou o tid dela, e sem tid não há para onde postar o WM_QUIT —
                # ela ficaria presa no GetMessageW com o hook instalado.
                self._ready.wait(2.0)
            tid = self._tid
            try:
                if tid and not u32.PostThreadMessageW(tid, WM_QUIT, 0, 0):
                    log.error("PostThreadMessageW(WM_QUIT) failed: %s",
                              ctypes.WinError(ctypes.get_last_error()))
            except Exception:
                log.exception("PostThreadMessageW(WM_QUIT) failed")
            self._tid = 0
        try:
            self.q.put(None)
        except Exception:
            log.exception("failed to stop hotkey worker")
        for t, label in ((hook_t, "hook"), (worker_t, "hk-worker")):
            # join() da própria thread levanta RuntimeError; daí o current_thread().
            try:
                if t is not None and t.is_alive() and t is not threading.current_thread():
                    t.join(2.0)
                    if t.is_alive():
                        log.warning("%s thread did not exit in 2s", label)
            except Exception:
                log.exception("joining the %s thread failed", label)
        with self._lk:
            self._hook_thread = None
            self._worker_thread = None

    @staticmethod
    def _check_inject_tag() -> None:
        """Import tardio de propósito: no topo do arquivo criaria ciclo com inject.py.
        Só confere que a tag duplicada aqui ainda bate com a de lá."""
        try:
            from wispr import inject
        except ImportError:
            log.debug("wispr.inject not importable yet; INJECT_TAG not cross-checked")
            return
        except Exception:
            log.exception("importing wispr.inject to cross-check INJECT_TAG failed")
            return
        try:
            if inject.INJECT_TAG not in IGNORED_TAGS:
                log.error("inject.INJECT_TAG=0x%X is not in IGNORED_TAGS: the app will "
                          "retrigger itself while typing", inject.INJECT_TAG)
        except Exception:
            log.exception("checking inject.INJECT_TAG failed")


if __name__ == "__main__":
    # Sem hook e sem injeção: só mostra como o chord configurado foi interpretado.
    _cfg = config.load()
    _spec = str(_cfg.get("hotkey", config.DEFAULTS["hotkey"]))
    try:
        _mods, _vk = parse_chord(_spec)
    except ValueError as _exc:
        print("invalid hotkey %r (%s); using default" % (_spec, _exc))
        _spec = config.DEFAULTS["hotkey"]
        _mods, _vk = parse_chord(_spec)
    print("hotkey      :", _spec)
    print("modifiers   :", sorted(_mods))
    print("key vk      : 0x%02X" % _vk)
    print("mask key    :", "0x%02X" % VK_MASK if "win" in _mods else "(none)")
    print("stop vk     : 0x%02X" % (_key_vk(str(_cfg.get("stop_key", "enter"))) or 0x0D))
    print("cancel vk   : 0x%02X" % (_key_vk(str(_cfg.get("cancel_key", "esc"))) or 0x1B))
    print("altgr filter:", "on (AltGr do ABNT2 colide com este chord)"
          if {"ctrl", "alt"} <= set(_mods) else "off (chord não colide com AltGr)")
    print("sizeof INPUT:", _INPUT_SIZE)
    print("ignored tags:", ["0x%X" % t for t in sorted(IGNORED_TAGS)])
    print("elevated    :", is_elevated())
