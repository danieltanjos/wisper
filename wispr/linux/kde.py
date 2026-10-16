# -*- coding: utf-8 -*-
"""Atalhos globais pelo kglobalaccel do KDE, via D-Bus (jeepney, Python puro).

No Wayland nenhum app comum ve tecla de outra janela: nao existe
`WH_KEYBOARD_LL`. Quem pode e' o compositor, e no Plasma o dono dos atalhos
globais e' o kglobalaccel (`org.kde.kglobalaccel`, `/kglobalaccel`). Um app
registra uma "acao" num "componente" e recebe o sinal `globalShortcutPressed`
em `/component/<componente>` — a tecla e' consumida pelo KWin e NAO chega a
janela em foco, igual ao `return 1` do hook do Windows.

Medido nesta maquina (Plasma 6.6.4, 2026-10-15), com F24 (que nao existe no
teclado, para nao roubar tecla de ninguem):
- tecla sem modificador e' aceita (F24 puro registrou e disparou);
- `invokeShortcut` -> `globalShortcutPressed` chega em < 1 ms;
- **o atalho CONTINUA ATIVO depois que o processo dono morre**
  (`isActive` True, `getGlobalShortcutsByKey` ainda acha). Para Meta+A isso e'
  so' uma tecla morta; para Return e Escape seria o Enter do usuario engolido
  no sistema inteiro. Daí o guardiao (wispr/linux/keyguard.py) e o desarme no
  boot. `setShortcut([])` + `setInactive` solta de verdade (conferido).

Codigos de tecla sao os do Qt (QKeySequence como int): modificadores somados
com o codigo da tecla. Ver https://doc.qt.io/qt-6/qt.html#Key-enum.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SERVICE = "org.kde.kglobalaccel"
PATH = "/kglobalaccel"
IFACE = "org.kde.KGlobalAccel"
COMPONENT_IFACE = "org.kde.kglobalaccel.Component"

COMPONENT = "wisper"
COMPONENT_FRIENDLY = "wisper (ditado por voz)"

# KGlobalAccel::SetShortcutFlag
IS_DEFAULT = 0x1
SET_PRESENT = 0x2
NO_AUTOLOADING = 0x4

# Qt::KeyboardModifier
QT_SHIFT = 0x02000000
QT_CTRL = 0x04000000
QT_ALT = 0x08000000
QT_META = 0x10000000
QT_KEYPAD = 0x20000000

QT_MODS = {
    "win": QT_META, "super": QT_META, "meta": QT_META, "cmd": QT_META, "windows": QT_META,
    "ctrl": QT_CTRL, "control": QT_CTRL, "ctl": QT_CTRL,
    "alt": QT_ALT, "menu": QT_ALT, "altgr": QT_ALT,
    "shift": QT_SHIFT, "shft": QT_SHIFT,
}

QT_KEYS = {
    "esc": 0x01000000, "escape": 0x01000000, "tab": 0x01000001,
    "backspace": 0x01000003, "enter": 0x01000004, "return": 0x01000004,
    "insert": 0x01000006, "delete": 0x01000007, "del": 0x01000007,
    "pause": 0x01000008, "printscreen": 0x01000009, "print": 0x01000009,
    "home": 0x01000010, "end": 0x01000011, "left": 0x01000012, "up": 0x01000013,
    "right": 0x01000014, "down": 0x01000015, "pageup": 0x01000016,
    "pagedown": 0x01000017, "capslock": 0x01000024, "scrolllock": 0x01000026,
    "space": 0x20, "spacebar": 0x20, "apps": 0x01000055,
}
QT_KEYS.update({"f%d" % i: 0x01000030 + i - 1 for i in range(1, 36)})   # F1..F35

KEY_RETURN = 0x01000004
KEY_ENTER = 0x01000005      # o Enter do teclado numerico, com QT_KEYPAD


def qt_key(name: str) -> int | None:
    """Nome de tecla -> codigo Qt (sem modificador). None se nao souber."""
    n = (name or "").strip().lower()
    if not n:
        return None
    if len(n) == 1 and ("a" <= n <= "z" or "0" <= n <= "9"):
        return ord(n.upper())
    return QT_KEYS.get(n)


def parse_chord(spec: str) -> int:
    """'win+a' -> QT_META | Qt::Key_A. Levanta ValueError no que nao der.

    Mesmas regras do `wispr.hotkey.parse_chord` do Windows: ao menos um
    modificador, os mesmos apelidos (win/super/meta, ctrl/control, ...).
    """
    parts = [p for p in (spec or "").strip().lower().replace("-", "+").split("+") if p]
    if len(parts) < 2:
        raise ValueError("chord precisa de ao menos um modificador e uma tecla: %r" % spec)
    mods = 0
    for p in parts[:-1]:
        m = QT_MODS.get(p)
        if m is None:
            raise ValueError("modificador desconhecido em %r: %r" % (spec, p))
        mods |= m
    key = qt_key(parts[-1])
    if key is None:
        raise ValueError("tecla desconhecida em %r: %r" % (spec, parts[-1]))
    return mods | key


def single_key_codes(name: str) -> list[int]:
    """Tecla de parar/cancelar (sem modificador) -> lista de codigos Qt.

    Enter vira os DOIS Enters: o principal (Key_Return) e o do teclado numerico
    (Keypad|Key_Enter). No Windows os dois sao VK_RETURN; no Qt sao teclas
    diferentes e o usuario nao deveria ter que saber disso.
    """
    code = qt_key(name)
    if code is None:
        raise ValueError("tecla desconhecida: %r" % name)
    if code == KEY_RETURN:
        return [KEY_RETURN, QT_KEYPAD | KEY_ENTER]
    return [code]


def describe(code: int) -> str:
    """Codigo Qt -> 'Meta+A', so para log e menu."""
    names = []
    for bit, label in ((QT_CTRL, "Ctrl"), (QT_ALT, "Alt"), (QT_SHIFT, "Shift"),
                       (QT_META, "Meta"), (QT_KEYPAD, "Num")):
        if code & bit:
            names.append(label)
    key = code & 0x01FFFFFF
    rev = {v: k for k, v in QT_KEYS.items()}
    if key == KEY_ENTER:
        label = "Enter"
    elif 0x20 < key < 0x7F:
        label = chr(key)
    else:
        label = rev.get(key, "0x%X" % key).capitalize()
    return "+".join(names + [label])


def action_id(action: str, friendly: str = "") -> list[str]:
    """O `actionId` do kglobalaccel: [componente, acao, nome do componente, nome da acao]."""
    return [COMPONENT, action, COMPONENT_FRIENDLY, friendly or action]


class KGlobalAccel:
    """Chamadas ao kglobalaccel sobre uma conexao jeepney ja aberta.

    `conn` e' qualquer objeto com `send_and_get_reply(msg, timeout=...)`: o
    `DBusRouter` de jeepney.io.threading (seguro entre threads) no app, ou a
    conexao bloqueante simples no guardiao.
    """

    def __init__(self, conn, timeout: float = 3.0):
        from jeepney import DBusAddress
        self.conn = conn
        self.timeout = timeout
        self.addr = DBusAddress(PATH, bus_name=SERVICE, interface=IFACE)

    def _call(self, method: str, signature: str, *args, addr=None):
        from jeepney import new_method_call
        from jeepney.wrappers import unwrap_msg
        msg = new_method_call(addr or self.addr, method, signature, args)
        reply = self.conn.send_and_get_reply(msg, timeout=self.timeout)
        return unwrap_msg(reply)

    def component_address(self, path: str | None = None):
        from jeepney import DBusAddress
        return DBusAddress(path or ("/component/" + COMPONENT), bus_name=SERVICE,
                           interface=COMPONENT_IFACE)

    def register(self, aid: list[str]) -> None:
        self._call("doRegister", "as", aid)

    def set_keys(self, aid: list[str], keys: list[int], flags: int) -> list[int]:
        """Devolve as teclas que o kglobalaccel REALMENTE gravou (pode diferir:
        atalho ja usado por outro componente volta vazio, e sem NO_AUTOLOADING
        vale o que o usuario configurou nas Configuracoes do Sistema)."""
        out = self._call("setShortcut", "asaiu", aid, [int(k) for k in keys], int(flags))
        return [int(k) for k in (out[0] if out else [])]

    def keys(self, aid: list[str]) -> list[int]:
        out = self._call("shortcut", "as", aid)
        return [int(k) for k in (out[0] if out else [])]

    def set_inactive(self, aid: list[str]) -> None:
        self._call("setInactive", "as", aid)

    def component_path(self) -> str:
        return str(self._call("getComponent", "s", COMPONENT)[0])

    def is_active(self, path: str | None = None) -> bool:
        return bool(self._call("isActive", "", addr=self.component_address(path))[0])

    def owners_of(self, key: int) -> list[tuple[str, str]]:
        """(componente, acao) que ja usam `key` — diagnostico de conflito."""
        out = self._call("getGlobalShortcutsByKey", "i", int(key))
        return [(str(info[2]), str(info[0])) for info in (out[0] if out else [])]

    # ------------------------------------------------------------ alto nivel
    def arm(self, aid: list[str], keys: list[int]) -> list[int]:
        """Liga um atalho temporario (Return/Escape), por cima do que estiver salvo."""
        self.register(aid)
        return self.set_keys(aid, keys, SET_PRESENT | NO_AUTOLOADING)

    def disarm(self, aid: list[str]) -> None:
        """Solta de verdade: sem tecla E inativo. So um dos dois nao basta para
        o caso do processo morto (ver docstring do modulo)."""
        self.set_keys(aid, [], NO_AUTOLOADING)
        self.set_inactive(aid)
