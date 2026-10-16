# -*- coding: utf-8 -*-
"""Area de transferencia no Wayland, via wl-clipboard (`wl-copy` / `wl-paste`).

O KWin 6.6 anuncia `ext_data_control_manager_v1` (conferido com `wayland-info`),
entao o `wl-copy` escreve e o `wl-paste` le SEM precisar de foco nem de janela
nossa. Sem esse protocolo o wl-clipboard abriria uma janela invisivel por um
instante para ganhar foco, e isso roubaria o foco da janela do usuario
justamente antes do Ctrl+V.

Duas selecoes: `clipboard` (Ctrl+C/Ctrl+V) e `primary` (selecao do mouse).
O colar padrao do wisper no Linux e' Shift+Insert, e em alguns terminais
(Alacritty, xterm, foot) Shift+Insert cola a PRIMARY, nao a clipboard. Por isso
o texto vai para as duas. Ver wispr/linux/inject.py.

Pacote de sistema: `wl-clipboard` (ja vem no Fedora KDE).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)

TIMEOUT_S = 3.0
TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING",
              "TEXT", "text/plain;charset=UTF-8")


def available() -> bool:
    return bool(shutil.which("wl-copy") and shutil.which("wl-paste"))


def _args(primary: bool) -> list[str]:
    return ["--primary"] if primary else []


def set_text(text: str, primary: bool = False) -> None:
    """Poe `text` na selecao. Levanta OSError se o wl-copy falhar.

    O wl-copy sobe um processo em segundo plano que serve a selecao ate' alguem
    trocar de dono; o processo que chamamos sai assim que a posse foi tomada.
    Esse filho herda stdout/stderr: com stderr=PIPE o run() espera o EOF que so'
    vem quando a selecao troca de dono e estoura o timeout (medido: 3 s sempre).
    Por isso o stderr vai para um arquivo temporario, nunca para um pipe.
    """
    exe = shutil.which("wl-copy")
    if not exe:
        raise OSError("wl-copy nao encontrado (sudo dnf install wl-clipboard)")
    with tempfile.TemporaryFile() as err:
        try:
            r = subprocess.run([exe, *_args(primary)], input=str(text).encode("utf-8"),
                               stdout=subprocess.DEVNULL, stderr=err,
                               timeout=TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError("wl-copy falhou: %s" % exc) from exc
        if r.returncode != 0:
            err.seek(0)
            raise OSError("wl-copy saiu com %d: %s" % (r.returncode,
                                                       err.read().decode("utf-8", "replace").strip()))


def clear(primary: bool = False) -> None:
    exe = shutil.which("wl-copy")
    if not exe:
        raise OSError("wl-copy nao encontrado")
    try:
        subprocess.run([exe, *_args(primary), "--clear"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("wl-copy --clear falhou: %s" % exc) from exc


def list_types(primary: bool = False) -> list[str] | None:
    """Tipos MIME oferecidos agora. [] = selecao vazia; None = nao deu para ler."""
    exe = shutil.which("wl-paste")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *_args(primary), "--list-types"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        log.debug("wl-paste --list-types failed", exc_info=True)
        return None
    if r.returncode != 0:
        # "No selection" sai com 1: selecao vazia, nao erro.
        return []
    return [t.strip() for t in r.stdout.decode("utf-8", "replace").splitlines() if t.strip()]


def has_text_type(types) -> bool:
    return any(t in TEXT_TYPES or t.startswith("text/") for t in (types or ()))


def get_text(primary: bool = False) -> str | None:
    """Texto da selecao, ou None se nao houver texto (ou nao der para ler)."""
    exe = shutil.which("wl-paste")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *_args(primary), "--no-newline", "--type", "text"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        log.debug("wl-paste failed", exc_info=True)
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")
