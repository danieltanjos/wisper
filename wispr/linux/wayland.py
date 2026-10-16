# -*- coding: utf-8 -*-
"""Cliente Wayland minimo, em Python puro, so para o `org_kde_kwin_fake_input`.

Por que isto existe: no Wayland um app comum nao injeta tecla em ninguem. Os
caminhos medidos nesta maquina (Fedora 44, KWin 6.6.4), em 2026-10-15:

- `wtype` precisa de `zwp_virtual_keyboard_manager_v1`, e o KWin NAO anuncia
  esse global para cliente comum (conferido com `wayland-info`).
- `ydotool` / `/dev/uinput` exigem root (udev + grupo). Ver
  scripts/setup-linux-root.sh, que fica como plano B e nao e' rodado.
- `org_kde_kwin_fake_input` (v6) e' um global RESTRITO do KWin: ele so aparece
  para o executavel que um `.desktop` declara com
  `X-KDE-Wayland-Interfaces=org_kde_kwin_fake_input`. Medido: um `.desktop` em
  `~/.local/share/applications` (usuario, sem root) basta — o KWin casa o
  `Exec=` com o `/proc/<pid>/exe` do cliente. E' o caminho do KDE Connect.

O `/proc/<pid>/exe` de um script Python e' o interpretador. Para nao liberar o
fake_input para TODO `python3.14` da maquina, o scripts/setup.sh copia o
binario do Python para `.venv/bin/wisper` (copia real, nao symlink) e o `.desktop`
aponta para essa copia. So o processo do wisper ganha o global.

Protocolo (fake-input.xml do plasma-wayland-protocols; opcodes conferidos na
tabela `wl_message` do libkwin.so.6.6.4):
    0 authenticate(application: string, reason: string)
   10 keyboard_key(button: uint, state: uint)        # since 4; button = keycode evdev
   11 destroy()                                      # since 5
   12 keyboard_keysym(keysym: uint, state: uint)     # since 6

Teclas sao keycodes evdev (fisicos): Shift/Insert/Ctrl/V nao mudam entre US e
ABNT2, entao o atalho de colar e' independente de layout. Texto com acento NAO
e' digitado por aqui (ver wispr/linux/inject.py).
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import time

log = logging.getLogger(__name__)

FAKE_INPUT = "org_kde_kwin_fake_input"

# opcodes de request
_DISPLAY_SYNC, _DISPLAY_GET_REGISTRY = 0, 1
_REGISTRY_BIND = 0
_FI_AUTHENTICATE, _FI_KEYBOARD_KEY, _FI_DESTROY = 0, 10, 11
# opcodes de evento
_EV_DISPLAY_ERROR, _EV_DISPLAY_DELETE_ID = 0, 1
_EV_REGISTRY_GLOBAL = 0
_EV_CALLBACK_DONE = 0

KEY_PRESSED, KEY_RELEASED = 1, 0

# keycodes evdev (linux/input-event-codes.h)
KEYCODES = {
    "esc": 1, "enter": 28, "ctrl": 29, "lctrl": 29, "shift": 42, "lshift": 42,
    "v": 47, "alt": 56, "lalt": 56, "space": 57, "insert": 110, "rctrl": 97,
    "rshift": 54, "meta": 125, "super": 125, "win": 125,
}
_MODS = ("ctrl", "lctrl", "rctrl", "shift", "lshift", "rshift", "alt", "lalt",
         "meta", "super", "win")


class WaylandError(OSError):
    """Falha de protocolo, de socket ou global ausente."""


def parse_chord(spec: str) -> list[int]:
    """'shift+insert' -> [42, 110]. Modificadores primeiro, na ordem dada.

    Levanta ValueError no que nao conhecer: um atalho de colar errado digitaria
    outra coisa na janela do usuario, e isso tem que estourar na configuracao.
    """
    parts = [p.strip().lower() for p in str(spec or "").replace("-", "+").split("+")
             if p.strip()]
    if not parts:
        raise ValueError("atalho de colar vazio")
    codes = []
    for p in parts:
        if p not in KEYCODES:
            raise ValueError("tecla desconhecida no atalho de colar %r: %r" % (spec, p))
        codes.append(KEYCODES[p])
    if parts[-1] in _MODS:
        raise ValueError("atalho de colar termina num modificador: %r" % spec)
    return codes


# ------------------------------------------------------------------ wire format
def _pad(n: int) -> int:
    return (n + 3) & ~3


def encode_string(s: str) -> bytes:
    """string do Wayland: u32 com o tamanho INCLUINDO o NUL, bytes, NUL, padding."""
    raw = s.encode("utf-8") + b"\0"
    return struct.pack("=I", len(raw)) + raw + b"\0" * (_pad(len(raw)) - len(raw))


def encode_message(obj_id: int, opcode: int, payload: bytes = b"") -> bytes:
    """Cabecalho de 8 bytes: id do objeto, depois (tamanho << 16) | opcode."""
    size = 8 + len(payload)
    if size > 0xFFFF:
        raise ValueError("mensagem Wayland grande demais: %d bytes" % size)
    return struct.pack("=II", obj_id, (size << 16) | opcode) + payload


def decode_string(buf: bytes, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("=I", buf, off)
    off += 4
    s = buf[off:off + max(0, n - 1)].decode("utf-8", "replace")
    return s, off + _pad(n)


def split_messages(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Separa (id, opcode, payload) do buffer; devolve tambem o resto incompleto."""
    out = []
    off = 0
    while len(buf) - off >= 8:
        obj, word = struct.unpack_from("=II", buf, off)
        size, opcode = word >> 16, word & 0xFFFF
        if size < 8:
            raise WaylandError("mensagem Wayland corrompida (size=%d)" % size)
        if len(buf) - off < size:
            break
        out.append((obj, opcode, buf[off + 8:off + size]))
        off += size
    return out, buf[off:]


def socket_path() -> str:
    name = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if os.path.isabs(name):
        return name
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not rt:
        raise WaylandError("XDG_RUNTIME_DIR nao definido: sem sessao Wayland")
    return os.path.join(rt, name)


# ------------------------------------------------------------------ conexao
class FakeInput:
    """Uma conexao curta: conecta, faz bind no fake_input, manda teclas, fecha.

    Curta de proposito: um KWin reiniciado (ou um logout/login) invalida
    qualquer conexao guardada, e conectar custa ~1 ms. Uso:

        with FakeInput() as fi:
            fi.chord([42, 110])
    """

    def __init__(self, app_name: str = "wisper", reason: str = "ditado por voz",
                 timeout: float = 2.0):
        self.app_name = app_name
        self.reason = reason
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.globals: dict[str, tuple[int, int]] = {}   # interface -> (name, version)
        self.fake = 0
        self.version = 0
        self._next_id = 2
        self._rx = b""

    # ---------------------------------------------------------------- baixo nivel
    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _send(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    def _roundtrip(self) -> None:
        """wl_display.sync e espera o done; erros de protocolo viram excecao."""
        cb = self._new_id()
        self._send(encode_message(1, _DISPLAY_SYNC, struct.pack("=I", cb)))
        deadline = time.monotonic() + self.timeout
        while True:
            for obj, op, payload in self._read(deadline):
                if obj == 1 and op == _EV_DISPLAY_ERROR:
                    oid, code = struct.unpack_from("=II", payload, 0)
                    msg, _ = decode_string(payload, 8)
                    raise WaylandError("erro do compositor no objeto %d (codigo %d): %s"
                                       % (oid, code, msg))
                if obj == 2 and op == _EV_REGISTRY_GLOBAL:
                    name, = struct.unpack_from("=I", payload, 0)
                    iface, off = decode_string(payload, 4)
                    ver, = struct.unpack_from("=I", payload, off)
                    self.globals[iface] = (name, ver)
                if obj == cb and op == _EV_CALLBACK_DONE:
                    return

    def _read(self, deadline: float):
        assert self.sock is not None
        left = deadline - time.monotonic()
        if left <= 0:
            raise WaylandError("compositor nao respondeu em %.1fs" % self.timeout)
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout as exc:
            raise WaylandError("compositor nao respondeu em %.1fs" % self.timeout) from exc
        if not chunk:
            raise WaylandError("o compositor fechou a conexao")
        msgs, self._rx = split_messages(self._rx + chunk)
        return msgs

    # ---------------------------------------------------------------- publico
    def connect(self) -> "FakeInput":
        path = socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            s.settimeout(self.timeout)
            s.connect(path)
        except OSError as exc:
            s.close()
            raise WaylandError("nao conectei em %s: %s" % (path, exc)) from exc
        self.sock = s
        self._send(encode_message(1, _DISPLAY_GET_REGISTRY, struct.pack("=I", 2)))
        self._next_id = 3
        self._roundtrip()
        if FAKE_INPUT not in self.globals:
            raise WaylandError(
                "o KWin nao anunciou %s para este processo (exe=%s): o .desktop com "
                "X-KDE-Wayland-Interfaces nao casou; rode scripts/setup.sh e inicie "
                "o wisper por .venv/bin/wisper" % (FAKE_INPUT, _exe()))
        name, ver = self.globals[FAKE_INPUT]
        self.version = min(ver, 6)
        if self.version < 4:
            raise WaylandError("%s v%d nao tem keyboard_key (precisa de v4)"
                               % (FAKE_INPUT, ver))
        self.fake = self._new_id()
        payload = (struct.pack("=I", name) + encode_string(FAKE_INPUT)
                   + struct.pack("=II", self.version, self.fake))
        self._send(encode_message(2, _REGISTRY_BIND, payload))
        self._send(encode_message(self.fake, _FI_AUTHENTICATE,
                                  encode_string(self.app_name) + encode_string(self.reason)))
        self._roundtrip()
        return self

    def key(self, code: int, pressed: bool) -> None:
        self._send(encode_message(self.fake, _FI_KEYBOARD_KEY,
                                  struct.pack("=II", int(code),
                                              KEY_PRESSED if pressed else KEY_RELEASED)))

    def chord(self, codes, hold: float = 0.012) -> None:
        """Aperta na ordem, solta na ordem inversa, e confirma com um roundtrip.

        `hold` entre os eventos: o KWin processa na ordem, mas alguns clientes
        (Electron, XWayland) perdem um modificador que sobe no mesmo quadro em
        que a tecla desce.
        """
        codes = [int(c) for c in codes]
        pressed = []
        try:
            for c in codes:
                self.key(c, True)
                pressed.append(c)
                time.sleep(hold)
        finally:
            # Soltar SEMPRE, mesmo com erro no meio: um Shift preso pelo
            # compositor estragaria tudo que o usuario digitar depois.
            for c in reversed(pressed):
                try:
                    self.key(c, False)
                    time.sleep(hold)
                except OSError:
                    log.exception("could not release fake key %d", c)
        self._roundtrip()

    def close(self) -> None:
        s, self.sock = self.sock, None
        if s is None:
            return
        try:
            if self.fake and self.version >= 5:
                s.sendall(encode_message(self.fake, _FI_DESTROY))
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def __enter__(self) -> "FakeInput":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()


def _exe() -> str:
    try:
        return os.readlink("/proc/self/exe")
    except OSError:
        return "?"


def probe(timeout: float = 2.0) -> tuple[bool, str]:
    """(o fake_input esta liberado para este processo?, motivo). Nao manda tecla.

    Conecta, faz bind, autentica e desconecta: zero eventos de teclado. Seguro
    de rodar com o usuario usando a maquina.
    """
    try:
        with FakeInput(timeout=timeout) as fi:
            return True, "%s v%d" % (FAKE_INPUT, fi.version)
    except Exception as exc:
        return False, str(exc)
