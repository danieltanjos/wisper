# -*- coding: utf-8 -*-
"""Injecao de texto no Linux (KDE Plasma 6 / Wayland): clipboard + atalho de colar.

Carregado como `wispr.inject` pelo finder do `wispr/__init__.py`; a API e' a do
docs/CONTRACT.md, a mesma do `wispr/inject.py` do Windows.

Por que NAO existe "digitar" aqui (o modo "type" do Windows):
o `SendInput` + `KEYEVENTF_UNICODE` do Windows e' independente de layout. No
Wayland o unico injetor sem root e' o `org_kde_kwin_fake_input` do KWin (ver
wispr/linux/wayland.py), e ele so fala keycode fisico ou keysym. Keysym de
acento nao tem tecla propria no ABNT2 desta maquina (`ã` e' tecla morta + `a`):
o KWin nao acha keycode e descarta o evento EM SILENCIO. Digitar pt-BR por ali
perderia letras sem erro nenhum. Entao TODO texto vai pelo clipboard, que e'
plano em qualquer tamanho e preserva qualquer caractere — e e' exatamente o que
o Wispr Flow faz (clipboard + colar de uma vez, ARCHITECTURE.md secao 7).

O atalho de colar e' `Shift+Insert` por padrao (`linux_paste_keys`), nao
Ctrl+V: no Konsole e em outros terminais Ctrl+V nao cola (manda ^V), e
Shift+Insert cola em Qt, GTK, Firefox, Chromium/Electron e no Konsole. Alguns
terminais (Alacritty, xterm, foot) colam a selecao PRIMARY com Shift+Insert,
por isso o texto vai tambem para a primary (`linux_paste_primary`).

Sem o fake_input liberado (scripts/setup.sh nao rodou, ou o wisper foi aberto
por outro executavel que nao o `.venv/bin/wisper`), o texto fica na area de
transferencia e o app avisa "use Ctrl+V" — o mesmo caminho do UIPI no Windows.

Nada aqui pode derrubar o app: toda falha e' logada e degradada.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from wispr import config
from wispr.delivery import Delivery, sanitize, utf16_units
from wispr.linux import clipboard, wayland

try:  # logging_setup e' de outro modulo; a ausencia dele nao pode derrubar nada
    from wispr import logging_setup as _logging_setup
    log = _logging_setup.get(__name__)
except Exception:  # pragma: no cover
    log = logging.getLogger(__name__)

__all__ = ["INJECT_TAG", "MSG_ELEVATED", "Delivery", "deliver", "type_unicode",
           "paste_text", "tap", "clip_get_text", "clip_set_text", "sanitize",
           "target_is_elevated", "injection_blocked", "utf16_units", "status"]

#: No Windows marca as teclas injetadas para o hook ignorar. No Linux o atalho e'
#: do kglobalaccel, que nunca ve as teclas do fake_input como chord nosso; a
#: constante fica pelo contrato e com o MESMO valor, para quem compara.
INJECT_TAG = 0x57495350
MSG_ELEVATED = "Janela em foco e de administrador; o texto nao pode ser inserido."

DEFAULT_PASTE_KEYS = "shift+insert"
# Medido no Windows: 20 ms ja colava o certo. No Wayland o app em foco le a
# selecao de forma ASSINCRONA, do processo do wl-copy, depois de receber a
# tecla; trocar a selecao antes disso cancela a transferencia. Piso maior.
MIN_RESTORE_DELAY = 0.15

# Tipos que um restore so de texto destroi (o equivalente ao CF_DIB do Windows):
# imagem, arquivo copiado no Dolphin (text/uri-list vira um caminho em texto).
_NONTEXT_PREFIXES = ("image/", "audio/", "video/", "application/x-qt-image")
_NONTEXT_TYPES = frozenset(("text/uri-list", "x-special/gnome-copied-files",
                            "application/x-kde-cutselection"))

_INJECT_LOCK = threading.RLock()
_LOCK_TIMEOUT = 20.0


@contextmanager
def _serialized(what):
    """Mesma regra do Windows: uma injecao por vez, nunca travar para sempre."""
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
# helpers de config (mesma semantica do Windows: "false" em string e' falso)
# --------------------------------------------------------------------------
_FALSE_WORDS = frozenset(("", "0", "false", "no", "nao", "off", "none"))


def _cfg_get(cfg, key):
    try:
        return cfg[key]
    except (KeyError, TypeError, IndexError):
        return config.DEFAULTS.get(key)


def _cfg_float(cfg, key, default):
    try:
        return float(_cfg_get(cfg, key))
    except (TypeError, ValueError):
        return float(default)


def _cfg_bool(cfg, key, default=False):
    value = _cfg_get(cfg, key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


def paste_keys(cfg=None) -> list[int]:
    """Keycodes evdev do atalho de colar. Config invalida cai no padrao, com log."""
    spec = _cfg_get(cfg, "linux_paste_keys") if cfg is not None else None
    spec = spec or DEFAULT_PASTE_KEYS
    try:
        return wayland.parse_chord(str(spec))
    except ValueError as exc:
        log.error("linux_paste_keys %r is invalid (%s); using %r", spec, exc,
                  DEFAULT_PASTE_KEYS)
        return wayland.parse_chord(DEFAULT_PASTE_KEYS)


def is_nontext(types) -> bool:
    """A selecao tem algo que um restore so de texto destruiria?"""
    for t in types or ():
        if t in _NONTEXT_TYPES or t.startswith(_NONTEXT_PREFIXES):
            return True
    return False


# --------------------------------------------------------------------------
# UIPI nao existe no Wayland
# --------------------------------------------------------------------------
def target_is_elevated():
    """Sempre False: no Wayland nao ha UIPI. Uma janela de root (pkexec, um
    `sudo kate`) recebe a colagem do compositor como qualquer outra."""
    return False


def injection_blocked():
    return False


# --------------------------------------------------------------------------
# clipboard
# --------------------------------------------------------------------------
def clip_get_text():
    return clipboard.get_text()


def clip_set_text(s):
    """Poe o texto na area de transferencia (so a clipboard, nao a primary:
    quem chama isto e' o fallback, e o usuario vai colar com Ctrl+V)."""
    clipboard.set_text("" if s is None else str(s))


def _snapshot():
    """(texto salvo ou None, havia algo nao-texto?) da clipboard."""
    types = clipboard.list_types()
    if not types:
        return None, False
    saved = clipboard.get_text() if clipboard.has_text_type(types) else None
    return saved, is_nontext(types)


# --------------------------------------------------------------------------
# teclas
# --------------------------------------------------------------------------
def tap(vk, mods=()):
    """Toque de uma tecla com modificadores. No Linux `vk` e `mods` sao
    KEYCODES EVDEV (wispr.linux.wayland.KEYCODES), nao virtual-keys do Windows.
    Levanta OSError (WaylandError) se o fake_input nao estiver liberado."""
    with wayland.FakeInput() as fi:
        fi.chord([*mods, vk])


def type_unicode(text, chunk_chars=64, gap=0.0005):
    """Nao existe no Wayland sem root: ver a docstring do modulo.

    Levanta OSError com `.sent = 0` (nada foi entregue), que e' o sinal do
    contrato para quem chama tentar outro caminho sem risco de duplicar.
    """
    err = OSError("digitar caractere a caractere nao e' suportado no Wayland; "
                  "o wisper cola pelo clipboard")
    err.sent = 0
    raise err


def paste_text(text, restore=True, settle=0.06, restore_delay=0.5, cfg=None):
    """Cola via clipboard + atalho de colar, restaurando o conteudo anterior.

    Retorna {"pasted": bool, "restored": bool, "lost_nontext_formats": bool},
    como no Windows. `pasted` False = o atalho nao foi enviado (fake_input
    indisponivel) e NADA foi entregue. Levanta OSError so quando nem o
    clipboard foi escrito.

    `pasted` True quer dizer que o KWin ACEITOU as teclas (roundtrip sem erro),
    nao que o app em foco colou: no Wayland ninguem confirma isso.
    """
    restore = bool(restore)
    try:
        settle = max(0.0, float(settle))
    except (TypeError, ValueError):
        settle = 0.06
    try:
        restore_delay = float(restore_delay)
    except (TypeError, ValueError):
        restore_delay = 0.5
    restore_delay = max(MIN_RESTORE_DELAY, restore_delay)
    primary = _cfg_bool(cfg, "linux_paste_primary", True)
    keys = paste_keys(cfg)

    saved, saved_primary, had_other, pasted, restored = None, None, False, False, False
    with _serialized("paste_text"):
        if restore:
            saved, had_other = _snapshot()
            if primary:
                saved_primary = clipboard.get_text(primary=True)
            if had_other:
                log.warning("paste_text: the clipboard held a non-text format (an image, "
                            "or files copied in Dolphin); the text-only restore destroys it")
        wrote = False
        try:
            clipboard.set_text(text)
            wrote = True
            if primary:
                try:
                    clipboard.set_text(text, primary=True)
                except OSError:
                    log.debug("paste_text: primary selection not set", exc_info=True)
            time.sleep(settle)
            try:
                with wayland.FakeInput() as fi:
                    fi.chord(keys)
                pasted = True
            except OSError as exc:
                log.warning("paste_text: paste keys not delivered: %s", exc)
        finally:
            if restore and wrote:
                if pasted:
                    time.sleep(restore_delay)
                if saved is not None:
                    try:
                        clipboard.set_text(saved)
                        restored = True
                    except OSError:
                        log.warning("paste_text: clipboard restore failed", exc_info=True)
                if primary and saved_primary is not None:
                    try:
                        clipboard.set_text(saved_primary, primary=True)
                    except OSError:
                        log.debug("paste_text: primary restore failed", exc_info=True)
    return {"pasted": pasted, "restored": restored, "lost_nontext_formats": had_other}


# --------------------------------------------------------------------------
# diagnostico (bandeja)
# --------------------------------------------------------------------------
_STATUS = {"t": 0.0, "ok": None, "why": ""}


def status(max_age: float = 60.0) -> tuple[bool | None, str]:
    """(fake_input liberado?, detalhe). Cacheado: a bandeja pergunta a cada
    abertura de menu e o probe abre uma conexao Wayland. Nao manda tecla."""
    now = time.monotonic()
    if _STATUS["ok"] is None or now - _STATUS["t"] > max_age:
        ok, why = wayland.probe()
        _STATUS.update(t=now, ok=ok, why=why)
    return _STATUS["ok"], _STATUS["why"]


# --------------------------------------------------------------------------
# entrega
# --------------------------------------------------------------------------
def deliver(text, cfg=None):
    """Entrega o texto transcrito na janela em foco. Nunca levanta.

    Sempre "paste" (ou "empty"). `chars` e' o total de code units UTF-16 quando
    o atalho de colar foi aceito, e 0 quando nao foi — o `app.py` cai no
    clipboard com aviso. Nao existe entrega parcial aqui: a colagem e' atomica.
    """
    mode, chars, lost = "paste", 0, False
    try:
        try:
            cfg = cfg or config.load()
        except Exception:
            log.exception("deliver: config.load failed, using defaults")
            cfg = config.DEFAULTS

        s = sanitize(text)
        if s and _cfg_bool(cfg, "trailing_space"):
            s += " "
        if not s:
            log.debug("deliver: empty text after sanitize, nothing to inject")
            return Delivery("empty", chars=0, target_elevated=None, blocked=None,
                            lost_nontext_formats=False, empty=True)
        want = str(_cfg_get(cfg, "inject_mode") or "auto").strip().lower()
        if want == "type":
            log.info("deliver: inject_mode=type does not exist on Wayland; pasting")

        total_units = len(utf16_units(s))
        with _serialized("deliver"):
            try:
                res = paste_text(s, restore=_cfg_bool(cfg, "restore_clipboard", True),
                                 restore_delay=_cfg_float(cfg, "clipboard_restore_delay", 0.5),
                                 cfg=cfg)
                lost = bool(res.get("lost_nontext_formats"))
                if res.get("pasted"):
                    chars = total_units
                else:
                    log.warning("deliver: paste keys did not go out; the app falls back "
                                "to leaving the text on the clipboard")
            except OSError as exc:
                log.warning("deliver: clipboard write failed (%s)", exc, exc_info=True)
    except Exception:
        log.exception("deliver: unexpected failure")

    return Delivery(mode, chars=chars, target_elevated=False, blocked=False,
                    lost_nontext_formats=lost, empty=False)
