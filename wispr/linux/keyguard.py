# -*- coding: utf-8 -*-
"""Guardiao dos atalhos: solta Return/Escape (e Meta+A) se o wisper morrer.

Medido (ver wispr/linux/kde.py): um atalho do kglobalaccel CONTINUA ATIVO depois
que o processo que o registrou morre. Enquanto o wisper grava, Return e Escape
sao atalhos globais dele; um crash nesse instante (segfault no ctranslate2, OOM
killer, `kill -9`) deixaria o Enter e o Esc do usuario engolidos no sistema
inteiro, sem ninguem ouvindo o sinal. `atexit` e handler de sinal nao rodam
nesses casos.

Por isso o `HotkeyEngine` sobe ESTE modulo como processo filho, em sessao
propria (`start_new_session`: um Ctrl+C no terminal do app nao o derruba
junto), com um pipe no stdin. O filho so le o stdin. Quando o pai morre, por
qualquer motivo, o kernel fecha o pipe, o `read()` volta com EOF e o guardiao
desarma os tres atalhos e sai. No desligamento normal o proprio engine ja
desarmou antes de fechar o pipe; o guardiao repete (idempotente) e sai.

Corrida com uma instancia NOVA: se o lock de instancia unica
(`config.LOCK_PATH`) estiver preso por outro processo quando o EOF chegar, um
wisper novo ja subiu e os atalhos sao DELE: o guardiao sai sem mexer em nada.

Uso interno:  python -m wispr.linux.keyguard          (le o stdin ate EOF)
Na mao:       python -m wispr.linux.keyguard --now    (solta ja; scripts/release-keys.sh)
              python -m wispr.linux.keyguard --forget (apaga o componente `wisper`
                                                      do kglobalaccel; desinstalacao)
"""
from __future__ import annotations

import fcntl
import os
import sys


def _lock_is_free(path: str) -> bool:
    """True se ninguem segura o lock de instancia unica agora."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def release_all(actions=("stop", "cancel", "dictate")) -> list[str]:
    """Desarma as acoes do componente `wisper`. Devolve as que falharam."""
    from jeepney.io.blocking import open_dbus_connection

    from wispr.linux import kde

    failed = []
    with open_dbus_connection(bus="SESSION") as conn:
        kga = kde.KGlobalAccel(conn)
        for action in actions:
            aid = kde.action_id(action)
            try:
                if action == "dictate":
                    # Meta+A fica salvo (e configuravel nas Configuracoes do
                    # Sistema); so deixa de ser escutado.
                    kga.set_inactive(aid)
                else:
                    kga.disarm(aid)
            except Exception:
                failed.append(action)
    return failed


def forget_all(actions=("stop", "cancel", "dictate")) -> list[str]:
    """Remove as acoes do kglobalaccel (e do kglobalshortcutsrc). Para quem
    desinstala: sem isto o grupo [wisper] fica para sempre nas Configuracoes."""
    from jeepney.io.blocking import open_dbus_connection

    from wispr.linux import kde

    failed = release_all(actions)
    with open_dbus_connection(bus="SESSION") as conn:
        kga = kde.KGlobalAccel(conn)
        for action in actions:
            try:
                kga._call("unregister", "ss", kde.COMPONENT, action)
            except Exception:
                failed.append(action)
    return failed


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--forget" in argv or "--now" in argv:
        fn = forget_all if "--forget" in argv else release_all
        try:
            failed = fn()
        except Exception as exc:
            print("wisper: kglobalaccel inacessivel: %r" % (exc,), file=sys.stderr)
            return 1
        if failed:
            print("wisper: falhou para %s" % ", ".join(sorted(set(failed))), file=sys.stderr)
            return 1
        print("wisper: atalhos soltos" if fn is release_all else "wisper: atalhos removidos")
        return 0
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    try:
        from wispr import config
        if not _lock_is_free(str(config.LOCK_PATH)):
            return 0
    except Exception:
        pass
    try:
        failed = release_all()
    except Exception:
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
