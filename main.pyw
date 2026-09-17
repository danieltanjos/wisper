# -*- coding: utf-8 -*-
"""Ponto de entrada do wisper sob pythonw.exe.

Extensao .pyw de proposito: o processo roda sem console. Isso significa que uma
excecao na subida nao aparece em lugar nenhum -- nem stdout, nem stderr, e o
logging pode nem ter sido configurado ainda. Por isso tudo aqui e envolvido num
try/except que grava o traceback em logs/crash.log e, em ultimo caso, mostra uma
MessageBoxW. Sem isso, "o programa simplesmente nao abre" e o usuario nao tem
uma unica pista do motivo.

Uso:  pythonw.exe main.pyw     (ou python.exe main.pyw para ver o console)
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Rodando por atalho, pela entrada do HKCU\...\Run ou por tarefa agendada, o cwd
# pode ser qualquer um; a raiz do projeto precisa estar no path para achar wispr/.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TITLE = "wisper - falha ao iniciar"

# O autostart e' uma entrada em HKCU\...\Run: uma falha de subida se repete a
# CADA logon, sempre com o mesmo traceback. Sem teto, o crash.log cresceria para
# sempre em modo append, e e' justamente o arquivo que alguem vai abrir para
# entender o problema. Mesma ideia do log rotativo de 1 MB, com um backup so.
_CRASH_MAX_BYTES = 256_000


def _log_dir() -> Path:
    """Pasta de logs pelo config, com a raiz do projeto como plano B."""
    try:
        from wispr import config
        return Path(config.LOG_DIR)
    except Exception:
        return ROOT / "logs"


def _roll(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _CRASH_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))   # substitui o backup anterior
    except OSError:
        pass          # nao vale perder o traceback por causa da rotacao dele


def _write_crash(text: str) -> Path | None:
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "crash.log"
        _roll(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n=== %s ===\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("executable: %s\n" % sys.executable)
            fh.write(text)
            fh.write("\n")
        return path
    except Exception:
        return None


def _mirror_to_log(text: str) -> None:
    """Repete o traceback no log do app, se ele ja existir.

    A falha pode acontecer depois de o logging estar de pe (o boot do App nao e'
    instantaneo), e quem investiga abre logs/wisper.log primeiro -- achar ali um
    "startup failed" que aponta para o crash.log evita a conclusao errada de que
    o processo nunca chegou a rodar.
    """
    try:
        import logging
        logger = logging.getLogger("wisper.main")
        if logging.getLogger().handlers or logger.handlers:
            logger.critical("startup failed; see logs/crash.log\n%s", text)
    except Exception:
        pass


def _message_box(text: str) -> None:
    """Ultimo recurso. Se nem o arquivo deu para escrever, ao menos aparece algo."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.UINT)
        user32.MessageBoxW.restype = ctypes.c_int
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
        user32.MessageBoxW(None, text[-1800:], _TITLE, 0x00000010 | 0x00010000 | 0x00040000)
    except Exception:
        pass


def main() -> int:
    try:
        from wispr.app import App
        return int(App().run() or 0)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    except KeyboardInterrupt:
        # Só acontece rodando por python.exe num console: é saída pedida, não
        # falha — não vale abrir uma caixa de erro modal por causa dela.
        return 0
    except BaseException:
        tb = traceback.format_exc()
        path = _write_crash(tb)
        _mirror_to_log(tb)
        onde = "Detalhes em:\n%s" % path if path else "Nao consegui nem gravar o crash.log."
        _message_box("O wisper nao conseguiu iniciar.\n\n%s\n\n%s" % (onde, tb))
        return 1


if __name__ == "__main__":
    sys.exit(main())
