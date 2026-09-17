# -*- coding: utf-8 -*-
"""Injecao de texto na janela em foco (Windows 11 x64, ctypes puro).

Hibrido medido em docs/ARCHITECTURE.md secao 5: `SendInput` com
`KEYEVENTF_UNICODE` para textos curtos e clipboard + `Ctrl+V` para textos
longos. `SendInput` cru faz 37-44k chars/s, mas a latencia *visivel* e de
~1,7 ms por caractere (250 chars = 0,55 s; 5000 chars = 10,1 s); colar e' plano
em ~80 ms para qualquer tamanho. Dai o corte em ~120 caracteres.

`KEYEVENTF_UNICODE` e' independente de layout: verificado nos tres layouts
instalados nesta maquina (en-US, ABNT2 0416:00000416 e pt-BR US-Intl
0416:00020409), sem composicao de dead key e com acentuacao pt-BR perfeita em
ida e volta. Nunca traduzir caractere para VK/scan code: ai o layout volta a
importar.

`deliver()` devolve a str "type", "paste" ou "empty" que o docs/CONTRACT.md
exige — o `wispr/app.py` faz `inject.deliver(...) or "type"` e compara com
"clipboard". Os diagnosticos extras (UIPI, formatos perdidos do clipboard,
quantas code units entraram de fato) viajam pendurados nessa mesma str, na
classe `Delivery`, sem quebrar a assinatura do contrato.

Nada aqui pode derrubar o app: o processo roda sob pythonw.exe, sem console,
entao toda falha e' logada e degradada.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import os
import struct
import threading
import time
from contextlib import contextmanager

from wispr import config

try:  # logging_setup e' de outro modulo; a ausencia dele nao pode derrubar nada
    from wispr import logging_setup as _logging_setup
    log = _logging_setup.get(__name__)
except Exception:  # pragma: no cover - so acontece em syntax check / teste solto
    log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_X64 = ctypes.sizeof(ctypes.c_void_p) == 8
ULONG_PTR = ctypes.c_ulonglong if _X64 else ctypes.c_ulong

# Mensagem pronta para quem chama mostrar quando o alvo e' elevado. O app.py tem
# a versao dele; esta existe para quem usar o modulo sozinho.
MSG_ELEVATED = "Janela em foco e de administrador; o texto nao pode ser inserido."


# --------------------------------------------------------------------------
# structs e prototipos
# --------------------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", w.DWORD)]


SIZEOF_INPUT = ctypes.sizeof(INPUT)

user32.SendInput.argtypes = (w.UINT, ctypes.c_void_p, ctypes.c_int)
user32.SendInput.restype = w.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = w.SHORT
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = (w.HWND, ctypes.POINTER(w.DWORD))
user32.GetWindowThreadProcessId.restype = w.DWORD

user32.OpenClipboard.argtypes = (w.HWND,)
user32.OpenClipboard.restype = w.BOOL
user32.CloseClipboard.argtypes = ()
user32.CloseClipboard.restype = w.BOOL
user32.EmptyClipboard.argtypes = ()
user32.EmptyClipboard.restype = w.BOOL
user32.EnumClipboardFormats.argtypes = (w.UINT,)
user32.EnumClipboardFormats.restype = w.UINT
user32.GetClipboardData.argtypes = (w.UINT,)
user32.GetClipboardData.restype = w.HANDLE
user32.SetClipboardData.argtypes = (w.UINT, w.HANDLE)
user32.SetClipboardData.restype = w.HANDLE

kernel32.GlobalAlloc.argtypes = (w.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = w.HGLOBAL
kernel32.GlobalLock.argtypes = (w.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (w.HGLOBAL,)
kernel32.GlobalUnlock.restype = w.BOOL
kernel32.GlobalFree.argtypes = (w.HGLOBAL,)
kernel32.GlobalFree.restype = w.HGLOBAL
kernel32.GlobalSize.argtypes = (w.HGLOBAL,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.CloseHandle.argtypes = (w.HANDLE,)
kernel32.CloseHandle.restype = w.BOOL
# restype obrigatorio nos dois: sem ele o HANDLE de 64 bits trunca em int de 32
# e o handle vira lixo — mesma armadilha do GetModuleHandleW na secao 3 do doc.
# GetCurrentProcess devolve o pseudo-handle (HANDLE)-1, o caso mais obvio de
# truncagem que existe.
kernel32.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
kernel32.OpenProcess.restype = w.HANDLE
kernel32.GetCurrentProcess.argtypes = ()
kernel32.GetCurrentProcess.restype = w.HANDLE

advapi32.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
advapi32.OpenProcessToken.restype = w.BOOL
advapi32.GetTokenInformation.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         w.DWORD, ctypes.POINTER(w.DWORD))
advapi32.GetTokenInformation.restype = w.BOOL


# --------------------------------------------------------------------------
# constantes
# --------------------------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_V = 0x56
# Tecla-mascara: VK sem funcao atribuida, usada para desarmar o menu Iniciar /
# a barra de menus antes de soltar Win ou Alt (docs/ARCHITECTURE.md secao 3).
VK_MASK = 0xE8

CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE = 1, 7, 13, 16
# CF_TEXT/CF_OEMTEXT/CF_LOCALE sao sintetizados pelo proprio Windows a partir de
# CF_UNICODETEXT: a presenca deles nao significa que havia outra coisa no
# clipboard. Qualquer formato fora deste conjunto (um CF_DIB, por exemplo) e'
# destruido por um restore so de texto.
_TEXT_FORMATS = frozenset((CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE))
GMEM_MOVEABLE = 0x0002

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
ERROR_ACCESS_DENIED = 5

INJECT_TAG = 0x57495350   # 'WISP' em dwExtraInfo: o nosso WH_KEYBOARD_LL ignora
                          # tudo que tenha essa tag, senao o app se retriggera.

SENDINPUT_MAX_CHARS = 120
# Medido: restore de clipboard com 0 ms faz o app colar o conteudo ANTIGO;
# 20 ms foi o menor valor que funcionou. Piso de seguranca contra config.json.
MIN_RESTORE_DELAY = 0.02

# Layout x64 de INPUT (sizeof == 40), byte-identico as structs do ctypes:
# 0 type | 4 pad | 8 wVk | 10 wScan | 12 dwFlags | 16 time | 20 pad | 24 dwExtraInfo | 32 pad
_FMT64 = "<I4xHHII4xQ8x"
# x86 nao acontece nesta maquina, mas o fallback tem que estar certo: 4 do type
# mais os 24 da uniao (MOUSEINPUT e o maior membro) = 28, nao 20.
_FMT32 = "<IHHIIL8x"
_FMT = _FMT64 if SIZEOF_INPUT == 40 else _FMT32
_PACK = struct.Struct(_FMT).pack
_LAYOUT_OK = struct.calcsize(_FMT) == SIZEOF_INPUT
if not _LAYOUT_OK:
    # Nao levantar no import: sob pythonw.exe isso seria uma morte invisivel.
    # _send_blob() recusa o envio, porque um buffer com passo errado faria o
    # SendInput ler campos aleatorios e mandar teclas aleatorias ao usuario.
    log.error("INPUT struct layout mismatch: sizeof=%d fmt=%d — injection disabled",
              SIZEOF_INPUT, struct.calcsize(_FMT))

# Uma injecao por vez no processo inteiro. O app.py dispara `repaste_last()`
# numa thread nova sem marcar estado, entao dois cliques seguidos no menu da
# bandeja chegariam aqui em paralelo — e duas gravacoes/restauracoes de
# clipboard intercaladas deixam a transcricao presa no clipboard do usuario.
_INJECT_LOCK = threading.RLock()
_LOCK_TIMEOUT = 20.0


@contextmanager
def _serialized(what):
    """Serializa a injecao entre threads, mas nunca trava para sempre.

    RLock porque `deliver()` chama `paste_text()`, que tambem precisa da trava.
    Se o timeout estourar, seguimos assim mesmo e registramos: perder o ditado
    e' pior que arriscar um clipboard intercalado, e um lock segurado por 20 s
    ja significa que outra coisa quebrou antes.
    """
    got = _INJECT_LOCK.acquire(timeout=_LOCK_TIMEOUT)
    if not got:
        log.error("%s: another injection still holds the lock after %.0fs; going anyway",
                  what, _LOCK_TIMEOUT)
    try:
        yield got
    finally:
        if got:
            _INJECT_LOCK.release()


# --------------------------------------------------------------------------
# helpers de config
# --------------------------------------------------------------------------
_FALSE_WORDS = frozenset(("", "0", "false", "no", "nao", "off", "none"))


def _cfg_get(cfg, key):
    try:
        return cfg[key]
    except (KeyError, TypeError, IndexError):
        return config.DEFAULTS.get(key)


def _cfg_int(cfg, key):
    try:
        return int(_cfg_get(cfg, key))
    except (TypeError, ValueError):
        return int(config.DEFAULTS[key])


def _cfg_float(cfg, key):
    try:
        return float(_cfg_get(cfg, key))
    except (TypeError, ValueError):
        return float(config.DEFAULTS[key])


def _cfg_bool(cfg, key):
    value = _cfg_get(cfg, key)
    # config.json editado a mao costuma trazer "false" como string, e string
    # nao vazia e' verdadeira em Python: seria um "restore_clipboard: false"
    # que restaura assim mesmo.
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


# --------------------------------------------------------------------------
# SendInput
# --------------------------------------------------------------------------
def _blob_unicode(code_units):
    """Monta o buffer de INPUTs para uma sequencia de code units UTF-16."""
    out = bytearray()
    dn = KEYEVENTF_UNICODE
    up = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    for u in code_units:
        out += _PACK(INPUT_KEYBOARD, 0, u, dn, 0, INJECT_TAG)
        out += _PACK(INPUT_KEYBOARD, 0, u, up, 0, INJECT_TAG)
    return bytes(out)


def _blob_vk(pairs):
    """pairs = [(vk, is_keyup), ...] -> buffer de INPUTs."""
    out = bytearray()
    for vk, keyup in pairs:
        out += _PACK(INPUT_KEYBOARD, vk, 0, KEYEVENTF_KEYUP if keyup else 0, 0, INJECT_TAG)
    return bytes(out)


def _send_blob(blob):
    """Envia o buffer. Levanta OSError com `.sent` = quantos eventos entraram.

    `.sent == 0` significa "nada aconteceu" e autoriza quem chama a tentar outro
    caminho; `.sent > 0` significa entrega parcial, e ai repetir duplicaria
    texto ou deixaria modificador preso.

    Sob UIPI a falha vem SEM codigo de erro: ai a OSError levantada nao e um
    WinError e traz a explicacao no lugar dele. Quem trata so olha `.sent`.
    """
    if not _LAYOUT_OK:
        raise OSError("INPUT layout mismatch; refusing to call SendInput")
    n = len(blob) // SIZEOF_INPUT
    if n == 0:
        return 0
    buf = ctypes.create_string_buffer(blob, len(blob))
    # Zerar antes da chamada: sem isto o `get_last_error()` abaixo poderia
    # devolver o erro de alguma chamada anterior e apontar para a causa errada.
    ctypes.set_last_error(0)
    sent = user32.SendInput(n, buf, SIZEOF_INPUT)
    if sent != n:
        code = ctypes.get_last_error()
        if code:
            err = ctypes.WinError(code)
        else:
            # Dentro de janela elevada o SendInput devolve 0 e NAO seta erro
            # nenhum: o UIPI descarta em silencio. Um ctypes.WinError(0) sairia
            # no log como "The operation completed successfully" bem na linha
            # que explica por que o texto nao apareceu. Ver injection_blocked().
            err = OSError(0, "SendInput accepted %d of %d events and reported no "
                             "error: the input was discarded silently, which "
                             "means UIPI (elevated foreground window, or the "
                             "secure desktop / UAC prompt is up)" % (sent, n))
        err.sent = sent
        raise err
    return sent


def utf16_units(text):
    """Vista das code units UTF-16-LE do texto (sem copia por caractere).

    `surrogatepass` de proposito: um surrogate solto (texto que nao passou por
    `sanitize()`) levantaria UnicodeEncodeError no meio da injecao e o ditado
    inteiro sumiria. Melhor perder um glifo.
    """
    return memoryview(text.encode("utf-16-le", "surrogatepass")).cast("H")


def type_unicode(text, chunk_chars=64, gap=0.0005):
    """Digita o texto com KEYEVENTF_UNICODE. Retorna as code units entregues.

    O texto sai em blocos de `chunk_chars` code units, e a contagem devolvida e'
    sempre ACUMULADA: no sucesso e' o total, e na falha ela vai pendurada em
    `.sent` da OSError levantada. Isso nao e' detalhe de log — e' a unica
    evidencia que o `deliver()` tem de que alguma coisa ja' entrou na janela.
    Enquanto `.sent` era o do bloco que falhou, "o bloco 1 entrou e o 2 foi
    descartado pelo UIPI" era indistinguivel de "nada entrou": o app via
    chars=0, caia no clipboard e o usuario colava os primeiros 64 caracteres
    DUAS vezes.

    Nunca traduz "\\n" para VK_RETURN de proposito: um Enter sintetico enviaria
    o chat ou submeteria o formulario em foco.
    """
    if not text:
        return 0
    try:
        chunk_chars = max(1, int(chunk_chars))
    except (TypeError, ValueError):
        chunk_chars = 64        # chunk 0 daria laco infinito com a janela travada
    units = list(utf16_units(text))
    i, n = 0, len(units)
    done = 0                    # code units confirmadas, acumuladas
    while i < n:
        j = min(i + chunk_chars, n)
        # Um par substituto (emoji) tem que ir no MESMO SendInput, senao o alvo
        # recebe dois caracteres invalidos no lugar do glifo.
        if j < n and 0xD800 <= units[j - 1] <= 0xDBFF:
            j += 1
        try:
            _send_blob(_blob_unicode(units[i:j]))
        except OSError as exc:
            # `_send_blob` conta EVENTOS do bloco dele, e cada code unit custa
            # dois (keydown + keyup). Divisao inteira de proposito: com um
            # keydown entregue e o keyup descartado, o caractere nao apareceu —
            # arredondar para cima contaria um glifo que ninguem viu.
            exc.sent = done + int(getattr(exc, "sent", 0) or 0) // 2
            raise
        done = j
        i = j
        if gap and i < n:
            time.sleep(gap)
    return done


def tap(vk, mods=()):
    """Pressiona e solta `vk` com os modificadores `mods` em volta."""
    mods = tuple(mods)
    seq = [(m, False) for m in mods]
    seq += [(vk, False), (vk, True)]
    seq += [(m, True) for m in reversed(mods)]
    try:
        _send_blob(_blob_vk(seq))
    except OSError as exc:
        # Envio parcial deixa o keyup no resto do blob que nao saiu, ou seja, o
        # modificador fica PRESO no sistema do usuario. Soltar antes de propagar.
        if getattr(exc, "sent", 0):
            try:
                _send_blob(_blob_vk([(vk, True)] + [(m, True) for m in reversed(mods)]))
            except OSError:
                log.error("tap: partial send and the cleanup keyup failed too; "
                          "vk=0x%02X mods=%r", vk, mods, exc_info=True)
        raise


# Ordem importa: Win por ultimo, porque a mascara tem que ser injetada enquanto
# ele ainda esta' pressionado.
_MOD_VKS = (VK_LSHIFT, VK_RSHIFT, VK_SHIFT,
            VK_LCONTROL, VK_RCONTROL, VK_CONTROL,
            VK_LMENU, VK_RMENU, VK_MENU,
            VK_LWIN, VK_RWIN)
_MENU_ARMING = frozenset((VK_LMENU, VK_RMENU, VK_MENU, VK_LWIN, VK_RWIN))
# VK generico -> as duas variantes fisicas. GetAsyncKeyState(VK_CONTROL) responde
# por qualquer um dos dois lados.
_SIDES = {VK_SHIFT: (VK_LSHIFT, VK_RSHIFT),
          VK_CONTROL: (VK_LCONTROL, VK_RCONTROL),
          VK_MENU: (VK_LMENU, VK_RMENU)}


def _is_down(vk):
    try:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)
    except OSError:
        return False


def _release_stuck_modifiers():
    """Solta os modificadores que o usuario ainda esteja segurando.

    O ponto mais perigoso do app inteiro. O chord e' Win+A e o usuario costuma
    ainda estar com o Win (ou o Ctrl, no chord alternativo Ctrl+Alt+A) abaixado
    quando o texto sai. Injetar caracteres com um modificador preso nao digita
    texto: vira uma saraivada de atalhos Ctrl+letra no app em foco. Por isso o
    keyup sintetico vem ANTES de qualquer injecao, e de novo antes do Ctrl+V.

    Solta as variantes L/R explicitamente: SendInput com o VK generico e scan 0
    e' normalizado para a tecla da esquerda, entao um AltGr (que e' RCtrl+RAlt
    no ABNT2) ficaria preso se so soltassemos VK_CONTROL/VK_MENU.

    Retorna quantos modificadores foram soltos.
    """
    try:
        down = [vk for vk in _MOD_VKS if _is_down(vk)]
        # O generico so entra se nenhum dos dois lados apareceu: senao o keyup
        # dele (normalizado para a esquerda) seria um evento fantasma de LShift/
        # LCtrl/LAlt que o usuario nunca apertou.
        held = [vk for vk in down
                if vk not in _SIDES or not any(s in down for s in _SIDES[vk])]
    except Exception:
        log.exception("_release_stuck_modifiers: state read failed")
        return 0
    if not held:
        return 0
    seq = []
    if any(vk in _MENU_ARMING for vk in held):
        # Win ou Alt sozinhos, ao subir, abrem o menu Iniciar / a barra de menus.
        # A tecla-mascara 0xE8 desarma os dois (docs/ARCHITECTURE.md secao 3).
        seq += [(VK_MASK, False), (VK_MASK, True)]
    seq += [(vk, True) for vk in held]
    try:
        _send_blob(_blob_vk(seq))
    except OSError:
        log.warning("_release_stuck_modifiers: SendInput failed", exc_info=True)
        return 0
    return len(held)


# --------------------------------------------------------------------------
# clipboard
# --------------------------------------------------------------------------
def _open_clip(retries=20, delay=0.03):
    """Abre o clipboard com retry: qualquer app pode estar segurando o lock."""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    raise ctypes.WinError(ctypes.get_last_error())


def _read_unicode_text():
    """CF_UNICODETEXT do clipboard JA ABERTO por quem chamou."""
    h = user32.GetClipboardData(CF_UNICODETEXT)
    if not h:
        return None
    p = kernel32.GlobalLock(h)
    if not p:
        return None
    try:
        # Limitado ao tamanho do bloco: um CF_UNICODETEXT sem NUL final faria o
        # wstring_at ler memoria alheia ate achar um zero.
        size = kernel32.GlobalSize(h)
        if size >= 2:
            return ctypes.wstring_at(p, size // 2).split("\x00", 1)[0]
        return ctypes.wstring_at(p)
    finally:
        kernel32.GlobalUnlock(h)


def clip_get_text():
    """Texto do clipboard, ou None se nao houver CF_UNICODETEXT.

    Degrada para None tambem quando o clipboard esta' preso por outro app: o
    contrato promete `str | None`, e derrubar o app por causa de um Word
    segurando o lock seria ridiculo.
    """
    try:
        _open_clip()
    except OSError:
        log.warning("clip_get_text: could not open the clipboard", exc_info=True)
        return None
    try:
        return _read_unicode_text()
    except Exception:
        log.warning("clip_get_text: unreadable clipboard content", exc_info=True)
        return None
    finally:
        user32.CloseClipboard()


def clip_set_text(s):
    """Coloca `s` no clipboard como CF_UNICODETEXT.

    Levanta OSError se nao conseguir — e' o unico jeito de avisar quem chama
    para cair no caminho de digitacao.
    """
    data = (str(s) + "\0").encode("utf-16-le", "surrogatepass")
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    p = kernel32.GlobalLock(h)
    if not p:
        err = ctypes.get_last_error()
        kernel32.GlobalFree(h)
        raise ctypes.WinError(err)
    try:
        ctypes.memmove(p, data, len(data))
    finally:
        kernel32.GlobalUnlock(h)
    try:
        _open_clip()
    except OSError:
        kernel32.GlobalFree(h)
        raise
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            err = ctypes.get_last_error()
            kernel32.GlobalFree(h)
            raise ctypes.WinError(err)
        # Sucesso: o bloco passou a ser do clipboard. GlobalFree aqui seria
        # use-after-free na proxima colagem de quem quer que seja.
    finally:
        user32.CloseClipboard()


def clip_formats():
    """Lista os formatos atualmente no clipboard ([] se nao der para abrir)."""
    try:
        _open_clip()
    except OSError:
        log.warning("clip_formats: could not open the clipboard", exc_info=True)
        return []
    try:
        out, f = [], 0
        while True:
            f = user32.EnumClipboardFormats(f)
            if not f:
                break
            out.append(f)
        return out
    finally:
        user32.CloseClipboard()


def _snapshot_clipboard():
    """(texto|None, tinha_formato_nao_texto), numa unica abertura.

    Duas aberturas seguidas (uma para enumerar, outra para ler) dobram a chance
    de perder a corrida para outro app e ainda podem ver conteudos diferentes.
    """
    text, other = None, False
    try:
        _open_clip()
    except OSError:
        log.warning("clipboard snapshot: could not open the clipboard", exc_info=True)
        return None, False
    try:
        f = 0
        while True:
            f = user32.EnumClipboardFormats(f)
            if not f:
                break
            if f not in _TEXT_FORMATS:
                other = True
        text = _read_unicode_text()
    except Exception:
        log.warning("clipboard snapshot failed", exc_info=True)
    finally:
        user32.CloseClipboard()
    return text, other


def paste_text(text, restore=True, settle=0.06, restore_delay=0.25):
    """Cola o texto via clipboard + Ctrl+V, restaurando o conteudo anterior.

    Retorna {"pasted": bool, "restored": bool, "lost_nontext_formats": bool}.
    `pasted` False quer dizer que o Ctrl+V nao chegou (UIPI, tipicamente) e que
    NADA foi entregue — quem chama pode tentar digitar sem risco de duplicar.
    `lost_nontext_formats` avisa que havia algo nao-texto no clipboard (uma
    imagem CF_DIB, por exemplo) que o restore so de CF_UNICODETEXT destroi.

    Levanta OSError so quando nem o clipboard foi escrito.
    """
    restore = bool(restore)
    try:
        settle = max(0.0, float(settle))
    except (TypeError, ValueError):
        settle = 0.06
    try:
        restore_delay = float(restore_delay)
    except (TypeError, ValueError):
        restore_delay = 0.25
    # Piso medido: abaixo de 20 ms o app em foco cola o clipboard ANTIGO.
    restore_delay = max(MIN_RESTORE_DELAY, restore_delay)

    saved, had_other, pasted, restored = None, False, False, False

    with _serialized("paste_text"):
        if restore:
            saved, had_other = _snapshot_clipboard()
            if had_other:
                # O README promete este aviso, entao ele e' emitido AQUI, onde o
                # caso e' detectado: depender de quem chama olhar o valor de
                # retorno ja' falhou uma vez. O EmptyClipboard de clip_set_text
                # leva tudo, e o restore so devolve CF_UNICODETEXT — uma imagem
                # CF_DIB copiada pelo usuario morre neste ponto.
                log.warning("paste_text: the clipboard held a non-text format "
                            "(an image, for instance); the text-only restore "
                            "destroys it")
        try:
            # Dentro do try: se o SetClipboardData falhar DEPOIS do
            # EmptyClipboard, o clipboard do usuario ficou vazio e so o finally
            # devolve o conteudo dele.
            clip_set_text(text)
            time.sleep(settle)          # o alvo precisa de um instante para ver o novo dono
            _release_stuck_modifiers()  # de novo: o settle deu tempo do usuario apertar algo
            try:
                tap(VK_V, mods=(VK_CONTROL,))
                pasted = True
            except OSError as exc:
                # UIPI: SendInput devolve 0 com GetLastError() == 0.
                pasted = bool(getattr(exc, "sent", 0))
                log.warning("paste_text: Ctrl+V was not delivered (sent=%s)",
                            getattr(exc, "sent", 0), exc_info=True)
        finally:
            # Restore no finally: se o Ctrl+V falhar, o clipboard do usuario nao
            # pode ficar com a nossa transcricao dentro.
            if restore and saved is not None:
                if pasted:
                    time.sleep(restore_delay)
                try:
                    clip_set_text(saved)
                    restored = True
                except OSError:
                    log.warning("paste_text: clipboard restore failed", exc_info=True)

    return {"pasted": pasted, "restored": restored, "lost_nontext_formats": had_other}


# --------------------------------------------------------------------------
# UIPI
# --------------------------------------------------------------------------
_SELF_ELEVATED = None   # cache; a corrida entre threads e' benigna (mesmo valor)


def _token_is_elevated(hproc):
    """True/False para o token de `hproc`, None quando nao da' para saber.
    Nao fecha `hproc`."""
    tok = w.HANDLE()
    if not advapi32.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        info = TOKEN_ELEVATION()
        ret = w.DWORD(0)
        ok = advapi32.GetTokenInformation(tok, TokenElevation, ctypes.byref(info),
                                          ctypes.sizeof(info), ctypes.byref(ret))
        if not ok:
            return None
        return bool(info.TokenIsElevated)
    finally:
        kernel32.CloseHandle(tok)


def _process_is_elevated(pid):
    """(elevado|None, negaram_o_acesso) para o processo `pid`."""
    ctypes.set_last_error(0)
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return _token_is_elevated(h), False
    finally:
        kernel32.CloseHandle(h)


def _self_is_elevated():
    """O proprio wisper esta' elevado? Medido uma vez e guardado."""
    global _SELF_ELEVATED
    if _SELF_ELEVATED is None:
        try:
            # Pseudo-handle (HANDLE)-1: nunca fechar, nunca guardar.
            _SELF_ELEVATED = bool(_token_is_elevated(kernel32.GetCurrentProcess()))
        except Exception:
            log.debug("could not read our own token", exc_info=True)
            _SELF_ELEVATED = False
    return _SELF_ELEVATED


def target_is_elevated():
    """A janela em foco roda elevada? None quando nao da' para determinar.

    Resposta crua, sem comparar com o nosso nivel: quem quer saber se a injecao
    vai passar usa `injection_blocked()`.
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        if pid.value == os.getpid():
            return False        # nossa propria overlay/bandeja em foco
        elevated, denied = _process_is_elevated(pid.value)
        if elevated is None and denied:
            # Nem abrir o processo para consulta conseguimos: sob UIPI isso so
            # acontece com quem esta' acima de nos.
            return True
        return elevated
    except Exception:
        log.debug("target_is_elevated: probe failed", exc_info=True)
        return None


def injection_blocked():
    """O UIPI vai descartar a injecao em SILENCIO? None = nao da' para saber.

    Existe porque dentro de uma janela elevada tanto o SendInput quanto o
    Ctrl+V somem com GetLastError() == 0: sem esta checagem o app acharia que
    entregou o texto e o usuario nao veria nada.

    So e' True quando o alvo esta' elevado E nos NAO estamos. Se o wisper subiu
    elevado (o caminho de tarefa agendada da secao 6 do doc) a injecao funciona
    normalmente, e avisar o usuario ali seria alarme falso.
    """
    target = target_is_elevated()
    if not target:
        return target           # False ou None, preservando o "nao sei"
    return not _self_is_elevated()


# --------------------------------------------------------------------------
# saneamento
# --------------------------------------------------------------------------
_CTRL_MAP = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CTRL_MAP.update({c: None for c in range(0x7F, 0xA0)})
# Surrogate solto: nao e' caractere nenhum, e o encode UTF-16 estrito do
# type_unicode estouraria em cima dele. Emoji de verdade nao passa por aqui —
# em Python eles sao um code point unico acima de 0xFFFF.
_CTRL_MAP.update({c: None for c in range(0xD800, 0xE000)})
_CTRL_MAP[0xFEFF] = None        # BOM/ZWNBSP: invisivel e quebra busca no destino
_CTRL_MAP[0x200B] = None        # zero width space
_CTRL_MAP[0x2028] = 0x0A        # line separator, tratado como Enter por varios apps
_CTRL_MAP[0x2029] = 0x0A        # paragraph separator, idem


def sanitize(text):
    """Deixa o texto seguro para injetar.

    Normaliza CRLF, remove caracteres de controle (menos \\n e \\t) e derruba a
    quebra de linha final. Essa ultima parte nao e' cosmetica: o usuario aperta
    Enter para PARAR a gravacao, entao um "\\n" no fim do texto vira um segundo
    Enter e envia sozinho o chat ou o formulario em foco.
    """
    if not text:
        return ""
    try:
        s = str(text)
    except Exception:
        log.warning("sanitize: unusable input %r", type(text))
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.translate(_CTRL_MAP)
    return s.strip()


# --------------------------------------------------------------------------
# entrega
# --------------------------------------------------------------------------
class Delivery(str):
    """O modo de entrega ("type", "paste" ou "empty") com os diagnosticos.

    E' uma `str` porque o docs/CONTRACT.md declara `deliver() -> str` e o
    `app.py` faz `inject.deliver(...) or "type"`, compara com "clipboard" e
    loga com %s. Tudo isso continua funcionando. Quem quiser mais le
    `res.blocked`, `res.chars`, ou no estilo dicionario `res["target_elevated"]`
    — indice inteiro e fatia continuam sendo os da string.

    Os seis atributos abaixo existem em TODO retorno de `deliver()`, inclusive
    nos retornos antecipados e nos caminhos de excecao:

    - `mode`: "type" | "paste" | "empty".
    - `chars`: code units UTF-16 que a injecao CONFIRMOU ter entregue. E'
      cumulativo mesmo quando a entrega falhou no meio; `chars > 0` com falha
      significa entrega PARCIAL, e ai repetir o texto duplica o pedaco que ja'
      entrou. Nunca e' "o tamanho do que a gente queria entregar".
    - `target_elevated`: a resposta CRUA de "a janela em foco e' de
      administrador?" (True | False | None = nao deu para saber). Sozinha ela
      NAO quer dizer que a injecao falha: se o wisper tambem estiver elevado,
      ela passa normalmente e agir sobre este campo e' alarme falso.
    - `blocked`: a resposta ACIONAVEL, "o UIPI vai engolir isto em silencio?"
      = alvo elevado E nos nao (True | False | None = nao deu para saber). E'
      este que autoriza cair para o clipboard, nunca o `target_elevated`.
    - `lost_nontext_formats`: o restore so de texto destruiu uma imagem
      (CF_DIB, por exemplo) que estava no clipboard do usuario.
    - `empty`: o texto morreu no `sanitize()` (era so' U+200B/BOM/controle) e
      nao havia nada para entregar. Distingue "nao havia texto" de "o texto
      nao entrou" — sem ele, um `chars <= 0` mandava a transcricao VAZIA para
      o clipboard do usuario e ainda avisava que a insercao falhou.

    NAO trocar por dict nem por str pura: o `app.py` le tudo como atributo, e
    ainda usa `.get()`.
    """

    def __new__(cls, mode, chars=0, target_elevated=None, blocked=None,
                lost_nontext_formats=False, empty=False):
        self = str.__new__(cls, mode)
        self.mode = str(mode)
        self.chars = int(chars)
        self.target_elevated = target_elevated
        self.blocked = blocked
        self.lost_nontext_formats = bool(lost_nontext_formats)
        self.empty = bool(empty)
        return self

    def as_dict(self):
        return {"mode": self.mode, "chars": self.chars,
                "target_elevated": self.target_elevated, "blocked": self.blocked,
                "lost_nontext_formats": self.lost_nontext_formats,
                "empty": self.empty}

    def keys(self):
        return self.as_dict().keys()

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.as_dict()[key]
        return str.__getitem__(self, key)

    def __repr__(self):
        return "<Delivery %r chars=%d blocked=%r lost=%r empty=%r>" % (
            self.mode, self.chars, self.blocked, self.lost_nontext_formats,
            self.empty)


def _pick_mode(cfg, s):
    """"type" ou "paste" conforme config + tamanho do texto."""
    mode = str(_cfg_get(cfg, "inject_mode") or "auto").strip().lower()
    if mode not in ("auto", "type", "paste"):
        log.warning("inject_mode %r is invalid, falling back to auto", mode)
        mode = "auto"
    if mode != "auto":
        return mode
    threshold = _cfg_int(cfg, "inject_threshold")
    if threshold < 0:
        threshold = SENDINPUT_MAX_CHARS
    # Texto com quebra de linha vai sempre pelo clipboard: no caminho digitado o
    # "\n" sai como caractere unicode e varios controles de edicao o descartam.
    return "paste" if (len(s) > threshold or "\n" in s) else "type"


def deliver(text, cfg=None):
    """Entrega o texto transcrito na janela em foco.

    Retorna "type", "paste" ou "empty" (uma `Delivery`, que e' str — ver a
    classe). `chars` e' sempre o numero de code units que a injecao CONFIRMOU
    ter entregue, inclusive nos caminhos de falha, e `blocked` vem preenchido em
    todo retorno (None quando nao deu para determinar).
    Nunca levanta: sob pythonw.exe uma excecao aqui seria invisivel.
    """
    mode, chars, lost = "type", 0, False
    target_elevated, blocked = None, None
    try:
        try:
            cfg = cfg or config.load()
        except Exception:
            log.exception("deliver: config.load failed, using defaults")
            cfg = config.DEFAULTS

        s = sanitize(text)
        if s and _cfg_bool(cfg, "trailing_space"):
            s += " "
        mode = _pick_mode(cfg, s)
        if not s:
            log.debug("deliver: empty text after sanitize, nothing to inject")
            # mode="empty" E empty=True: sem essa marca, "nao havia texto"
            # chegava ao app.py como chars=0, igualzinho a "a injecao falhou", e
            # um ditado que virou so' um U+200B mandava uma string VAZIA para o
            # clipboard do usuario com um "Nao consegui inserir o texto" junto.
            # Explicito de proposito: todo atributo da classe existe tambem nos
            # retornos antecipados, `blocked=None` = nao foi nem consultado.
            return Delivery("empty", chars=0, target_elevated=None, blocked=None,
                            lost_nontext_formats=False, empty=True)

        target_elevated = target_is_elevated()
        blocked = injection_blocked()
        if blocked:
            log.warning("foreground window is elevated and wisper is not: "
                        "UIPI drops SendInput and Ctrl+V silently")

        # Unidade unica para o `chars`: code units UTF-16, as mesmas que o
        # `type_unicode()` conta. Misturar len(s) com a contagem dele faria o
        # numero mudar de significado conforme o caminho.
        total_units = len(utf16_units(s))

        with _serialized("deliver"):
            typed = False       # ja' tentamos digitar? evita uma segunda passada
            try:
                _release_stuck_modifiers()
                if mode == "type":
                    typed = True
                    chars = type_unicode(s)
                else:
                    res = paste_text(s,
                                     restore=_cfg_bool(cfg, "restore_clipboard"),
                                     restore_delay=_cfg_float(cfg, "clipboard_restore_delay"))
                    lost = bool(res.get("lost_nontext_formats"))
                    if res.get("pasted"):
                        chars = total_units
                    else:
                        # O Ctrl+V nao chegou e nada foi entregue: digitar nao
                        # duplica nada. Sob UIPI vai falhar tambem, e ai fica no
                        # log com o aviso de janela elevada logo acima.
                        log.warning("deliver: paste did not land, typing instead")
                        _release_stuck_modifiers()
                        # `mode` muda ANTES da chamada: se o `type_unicode()`
                        # falhar no meio, quem le o resultado tem que saber que
                        # o que esta' na janela foi digitado, nao colado.
                        typed, mode = True, "type"
                        chars = type_unicode(s)
            except OSError as exc:
                # `.sent` do `type_unicode()` e' ACUMULADO; o do `paste_text()`
                # (falha de clipboard) simplesmente nao existe, e ai vale 0.
                sent = int(getattr(exc, "sent", 0) or 0)
                if sent > chars:
                    chars = sent
                log.warning("deliver: %s path failed after %d code units (%s)",
                            mode, chars, exc, exc_info=True)
                if chars:
                    # Entrega PARCIAL: o comeco do texto ja esta' na janela.
                    # Redigitar do zero repetiria esse pedaco — pior que falhar,
                    # porque o usuario nao tem como saber que houve duas
                    # passadas. Quem chama ve `chars > 0` e nao cola por cima.
                    log.error("deliver: partial delivery (%d of %d code units); "
                              "not retrying, a retry would duplicate text",
                              chars, total_units)
                elif mode == "paste" and not typed:
                    # Nada saiu e a digitacao ainda nao foi tentada: e' o unico
                    # caso em que repetir e' seguro.
                    try:
                        _release_stuck_modifiers()
                        typed, mode = True, "type"
                        chars = type_unicode(s)
                    except OSError as exc2:
                        chars = max(chars, int(getattr(exc2, "sent", 0) or 0))
                        log.exception("deliver: type fallback failed too "
                                      "(%d code units delivered)", chars)
    except Exception:
        log.exception("deliver: unexpected failure")

    # Unico retorno dos caminhos normais e de excecao: os seis atributos saem
    # preenchidos sempre, com `blocked=None` quando nem chegamos a consultar.
    return Delivery(mode, chars=chars, target_elevated=target_elevated,
                    blocked=blocked, lost_nontext_formats=lost, empty=False)
