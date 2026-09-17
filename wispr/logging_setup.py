# -*- coding: utf-8 -*-
"""Logging do wisper.

Este processo roda sob `pythonw.exe`: nao existe console, `sys.stderr` e None e
uma excecao nao tratada some sem deixar rastro nenhum para o usuario. Entao:

* tudo cai em logs/wisper.log (rotativo, 1 MB, 5 backups, utf-8);
* `sys.excepthook`, `threading.excepthook` e `sys.unraisablehook` sao
  instalados, porque o app tem quatro threads (principal/tray, hook, overlay,
  worker) e uma excecao em qualquer uma delas precisa aparecer no log;
* o `faulthandler` fica armado num arquivo separado, para um crash duro do
  CPython (ctranslate2, PortAudio, o hook de baixo nivel) deixar traceback.

Os handlers vao no logger RAIZ de proposito: audio.py e inject.py usam
`logging.getLogger(__name__)`, ou seja, a arvore 'wispr.*', enquanto setup()
devolve a arvore 'wisper'. Pendurar so em 'wisper' perderia metade do app.
NAO "corrija" isso: os dois caminhos de falha mais barulhentos do app inteiro
(microfone e injecao de texto) sumiriam do log por completo.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback

import wispr
from wispr import config

LOG_NAME = "wisper"
LOG_FILE = config.LOG_DIR / "wisper.log"
CRASH_FILE = config.LOG_DIR / "wisper-crash.log"
FORMAT = "%(asctime)s %(threadName)-10s %(levelname)-5s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5

# Bibliotecas que so falam besteira quando o nivel global e DEBUG.
# faster_whisper e ctranslate2 nao sao "quando DEBUG": o faster_whisper cospe
# duas linhas de INFO pelo logger dele em TODA transcricao ("Processing audio
# with duration...", "VAD filter removed...") e despeja cada segmento do VAD em
# DEBUG. Num log rotativo de 1 MB que existe para diagnosticar ESTE app, esse
# ruido empurra para fora justamente as linhas que importam.
_NOISY = ("comtypes", "urllib3", "PIL", "huggingface_hub", "filelock", "fsspec",
          "matplotlib", "faster_whisper", "ctranslate2")

# Um erro dentro do proprio logging nao pode virar excecao na thread que so
# queria logar: o "--- Logging error ---" iria para um stderr que nao existe.
# Setado ja aqui no import, e nao dentro de setup(), porque tray.py e stt.py
# pegam logger no import deles - ou seja, antes de setup() rodar.
logging.raiseExceptions = False
if sys.stderr is None:
    # O lastResort escreve em sys.stderr; com stderr None todo emit anterior ao
    # setup() estouraria dentro do handler. Sob pythonw ele nao serve para nada.
    logging.lastResort = logging.NullHandler()

# _lock protege so a montagem dos handlers. Os tratadores de crash abaixo
# (_report/_emergency) NUNCA podem pegar este lock: eles rodam em qualquer
# thread, inclusive enquanto setup() esta la dentro abrindo arquivo.
_lock = threading.RLock()
_configured = False
_crash_fp = None  # precisa sobreviver: ver _install_faulthandler()


# --------------------------------------------------------------------------- #
# API publica
# --------------------------------------------------------------------------- #
def setup(level: str = "INFO") -> logging.Logger:
    """Configura o logging do processo e devolve o logger raiz do app.

    RotatingFileHandler em LOG_DIR/wisper.log, 1 MB, 5 backups, utf-8, no
    formato '%(asctime)s %(threadName)-10s %(levelname)-5s %(name)s: %(message)s'.
    Instala tambem sys.excepthook e threading.excepthook para que excecao em
    qualquer thread apareca no log - sob pythonw.exe nao existe console.

    Idempotente: chamar duas vezes so ajusta o nivel, nunca duplica handler.
    """
    global _configured
    with _lock:
        root = logging.getLogger()
        lvl = _level_of(level)

        if not (_configured or _find_handler(root) is not None):
            logging.raiseExceptions = False
            try:
                config.ensure_dirs()
            except Exception:
                pass  # disco cheio / pasta somente leitura: degrada logo abaixo

            # No logger RAIZ, nao em LOG_NAME: `wispr/audio.py` e
            # `wispr/inject.py` pegam `logging.getLogger(__name__)` e caem na
            # arvore 'wispr.*', que nao passa por 'wisper'. Trocar por
            # `logging.getLogger(LOG_NAME).addHandler(...)` apaga do log a
            # captura de audio e a injecao de texto — exatamente o que a gente
            # mais precisa ler quando o ditado falha.
            root.addHandler(_make_file_handler())
            console = _make_console_handler()
            if console is not None:
                root.addHandler(console)
            _install_faulthandler()
            _configured = True

        root.setLevel(lvl)
        _mute_noisy(lvl)

        # Reinstalados a cada chamada de proposito: pystray, comtypes ou
        # faster_whisper podem ter sobrescrito sys.excepthook depois do boot, e
        # quem escreve por ultimo ganha. Reinstalar e idempotente e barato.
        _install_excepthooks()

        try:
            # warnings.warn() tambem escreve em stderr, que aqui e None.
            logging.captureWarnings(True)
        except Exception:
            pass

        return logging.getLogger(LOG_NAME)


def get(name: str) -> logging.Logger:
    """Logger filho: get('audio') -> 'wisper.audio'.

    Aceita tambem `__name__` ('wispr.audio'), que vira 'wisper.audio' em vez de
    'wisper.wispr.audio'.
    """
    raw = str(name or "").strip()
    if raw.startswith("wispr."):
        raw = raw[len("wispr."):].strip()
    elif raw in ("wispr", "__main__"):
        raw = "app"
    if not raw or raw == LOG_NAME or not raw.strip("."):
        # '', 'wisper' e casos degenerados como 'wispr.' ou '...' caem na raiz
        # do app em vez de criar um logger de nome quebrado ('wisper.').
        return logging.getLogger(LOG_NAME)
    if raw.startswith(LOG_NAME + "."):
        return logging.getLogger(raw)
    return logging.getLogger(LOG_NAME + "." + raw)


def log_environment(log: logging.Logger | None = None) -> None:
    """Despeja o ambiente no log. E a primeira coisa a olhar quando algo
    inexplicavel acontece: interpretador errado, pasta errada, COM em STA,
    processo sem elevacao."""
    if log is None or not hasattr(log, "info"):
        log = logging.getLogger(LOG_NAME)
    try:
        log.info("wisper %s starting | pid=%d",
                 getattr(wispr, "__version__", "?"), os.getpid())
        log.info("python: %s", " ".join(sys.version.split()))
        log.info("executable: %s", sys.executable)
        log.info("base_prefix: %s | venv=%s | frozen=%s",
                 sys.base_prefix,
                 sys.prefix != sys.base_prefix,
                 bool(getattr(sys, "frozen", False)))
        if _is_store_python():
            # O Python da Microsoft Store tem identidade de pacote e entrada
            # propria no ConsentStore do microfone: InputStream() trava para
            # sempre, sem excecao, esperando uma UI que nunca aparece, e depois
            # passa a falhar com -9999/-9996. Ele ainda redireciona escritas em
            # %LOCALAPPDATA%/%APPDATA% para o sandbox do pacote.
            # Ver docs/ARCHITECTURE.md secao 4.
            log.warning("MICROSOFT STORE PYTHON DETECTED: the microphone will "
                        "hang or fail with -9999; use python.org CPython 3.11")
        else:
            log.info("interpreter: not the Store build (ok)")
        log.info("windowed (pythonw, no stderr): %s", sys.stderr is None)
        log.info("platform: %s | machine=%s", platform.platform(), platform.machine())
        # Com uma janela elevada em foco o hook de baixo nivel recebe zero
        # eventos e o SendInput e descartado com GetLastError()==0 (UIPI).
        # Ver docs/ARCHITECTURE.md secoes 3 e 5.
        log.info("elevated: %s", _is_elevated())
        log.info("cwd: %s", os.getcwd())
        log.info("root: %s", config.ROOT)
        log.info("config: %s (exists=%s)",
                 config.CONFIG_PATH, config.CONFIG_PATH.exists())
        log.info("models (HF_HOME): %s | env=%s",
                 config.MODELS_DIR, os.environ.get("HF_HOME"))
        log.info("assets: %s", config.ASSETS_DIR)
        log.info("logs: %s | crash: %s", LOG_FILE, CRASH_FILE)
        log.info("mutex: %s", config.MUTEX_NAME)
        log.info("com: ready=%s coinit_flags=%s error=%s",
                 getattr(wispr, "COM_READY", None),
                 getattr(sys, "coinit_flags", None),
                 getattr(wispr, "COM_ERROR", None))
    except Exception as exc:
        # Diagnostico nunca pode ser a causa de um crash - nem dentro do
        # proprio tratador: `log` pode ser um objeto quebrado do chamador.
        info = (type(exc), exc, exc.__traceback__)
        try:
            log.exception("log_environment failed")
        except Exception:
            _report("log_environment failed", exc_info=info)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _level_of(level) -> int:
    if isinstance(level, bool):
        return logging.INFO
    if isinstance(level, int):
        return level
    value = getattr(logging, str(level).strip().upper(), None)
    return value if isinstance(value, int) and not isinstance(value, bool) else logging.INFO


def _find_handler(logger: logging.Logger):
    for handler in list(logger.handlers):
        if getattr(handler, "_wisper", False):
            return handler
    return None


def _has_live_handler() -> bool:
    """True se existe handler de verdade (nao NullHandler) na cadeia do app."""
    logger = logging.getLogger(LOG_NAME)
    while logger is not None:
        for handler in list(logger.handlers):
            if not isinstance(handler, logging.NullHandler):
                return True
        if not logger.propagate:
            return False
        logger = logger.parent
    return False


def _make_file_handler() -> logging.Handler:
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            # Caminho do Windows pode trazer surrogate solto; sem isto o record
            # inteiro seria descartado em silencio no encode.
            errors="backslashreplace",
        )
        handler.setFormatter(logging.Formatter(FORMAT))
    except Exception:
        # Sem arquivo de log o app ainda tem que subir: o usuario perde o log,
        # nao o ditado.
        handler = logging.NullHandler()
    handler._wisper = True
    return handler


def _make_console_handler() -> logging.Handler | None:
    # Sob pythonw.exe sys.stderr e None e um StreamHandler nele explodiria a
    # cada linha. So existe console quando rodamos pelo python.exe.
    stream = sys.stderr
    if stream is None or not hasattr(stream, "write"):
        return None
    try:
        handler: logging.Handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(FORMAT))
    except Exception:
        return None
    handler._wisper = True
    return handler


def _mute_noisy(lvl: int) -> None:
    for name in _NOISY:
        try:
            logging.getLogger(name).setLevel(max(lvl, logging.WARNING))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Excecoes nao tratadas
# --------------------------------------------------------------------------- #
def _install_excepthooks() -> None:
    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable_hook


def _sys_excepthook(exc_type, exc, tb) -> None:
    if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
        return
    _report("unhandled exception on thread %s",
            threading.current_thread().name,
            exc_info=(exc_type, exc, tb))


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    # Ler so o nome: guardar `args.thread` mantem a thread viva no GC.
    name = getattr(getattr(args, "thread", None), "name", "?")
    _report("unhandled exception on thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _unraisable_hook(args) -> None:
    # Excecao engolida em __del__ ou em callback de C (PortAudio, ctypes).
    # Sem este hook ela desaparece por completo sob pythonw.
    # Nunca formatar nem guardar `args.object`: o __repr__ dele roda durante a
    # finalizacao, pode levantar de novo e pode ressuscitar o objeto. So o nome
    # do tipo sai daqui.
    try:
        obj = getattr(args, "object", None)
        where = type(obj).__name__ if obj is not None else "?"
        del obj
    except Exception:
        where = "?"
    detail = getattr(args, "err_msg", None) or "unraisable exception"
    _report("%s in <%s>", detail, where,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _report(msg: str, *args, exc_info) -> None:
    delivered = False
    try:
        if _has_live_handler():
            logging.getLogger(LOG_NAME + ".crash").critical(
                msg, *args, exc_info=exc_info)
            delivered = True
    except Exception:
        delivered = False
    if not delivered:
        # Sem handler vivo o registro sumiria por completo: raiseExceptions
        # esta desligado e o lastResort escreveria num stderr inexistente.
        try:
            text = msg % args if args else msg
        except Exception:
            text = str(msg)
        _emergency(text, exc_info)


def _emergency(text: str, exc_info=None) -> None:
    """Ultimo recurso: escreve direto no arquivo de crash quando ate o logging
    falhou. Sem locks e sem depender de setup() ter rodado."""
    try:
        chunk = "\n--- %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        if exc_info:
            try:
                chunk += "".join(traceback.format_exception(*exc_info))
            except Exception:
                chunk += repr(exc_info) + "\n"
        try:
            os.makedirs(str(config.LOG_DIR), exist_ok=True)
        except Exception:
            pass
        # Modo "a" e O_APPEND: dois writers pequenos nao se picotam.
        with open(CRASH_FILE, "a", encoding="utf-8", errors="replace") as fp:
            fp.write(chunk)
    except Exception:
        pass


def _roll_crash_file() -> None:
    """O arquivo de crash fica aberto a vida inteira do processo, entao nao
    rotaciona sozinho. Corta uma unica vez no boot para nao crescer sem fim."""
    try:
        if CRASH_FILE.exists() and CRASH_FILE.stat().st_size > MAX_BYTES:
            os.replace(str(CRASH_FILE), str(CRASH_FILE) + ".1")
    except OSError:
        pass


def _install_faulthandler() -> None:
    global _crash_fp
    if _crash_fp is not None:
        return
    fp = None
    try:
        _roll_crash_file()
        # Binario e sem buffer: o faulthandler escreve no descritor por conta
        # propria, entao um buffer de texto do Python so produziria intercalacao
        # fora de ordem com o cabecalho abaixo.
        fp = open(CRASH_FILE, "ab", buffering=0)
        fp.write(("\n=== faulthandler armed %s pid=%d\n"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
                 .encode("utf-8", "replace"))
        faulthandler.enable(file=fp, all_threads=True)
    except Exception:
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        return
    # O faulthandler guarda o descritor, nao o objeto Python: sem manter esta
    # referencia viva o GC fecha o arquivo e o dump de um SIGSEGV vai para um
    # fd morto - que e exatamente quando a gente mais precisa dele.
    _crash_fp = fp


# --------------------------------------------------------------------------- #
# Ambiente
# --------------------------------------------------------------------------- #
_STORE_MARKERS = ("windowsapps", "pythonsoftwarefoundation")


def _is_store_python() -> bool:
    blob = (str(sys.base_prefix) + " " + str(sys.executable)).lower()
    return any(marker in blob for marker in _STORE_MARKERS)


def _is_elevated():
    """True se o processo esta elevado, None se nao deu para saber.

    Importa porque, com uma janela elevada em foco, o hook de baixo nivel
    recebe zero eventos e o SendInput e descartado em silencio (UIPI).
    Ver docs/ARCHITECTURE.md secoes 3 e 5.
    """
    k32 = None
    token = None
    try:
        import ctypes
        from ctypes import wintypes

        # WinDLL proprio, nao ctypes.windll: aquele e um cache global do
        # processo e setar restype/argtypes nele atropelaria os prototipos que
        # hotkey.py e inject.py configuram nos mesmos modulos.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)

        # Sem restype explicito o ctypes devolve c_int e o HANDLE de 64 bits
        # chega truncado. Ver docs/ARCHITECTURE.md secao 3.
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        TOKEN_QUERY = 0x0008
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY,
                                    ctypes.byref(token)):
            return None
        # TOKEN_ELEVATION e um unico DWORD (TokenIsElevated); TokenElevation=20.
        elevation = wintypes.DWORD(0)
        got = wintypes.DWORD(0)
        ok = adv.GetTokenInformation(token, 20, ctypes.byref(elevation),
                                     ctypes.sizeof(elevation), ctypes.byref(got))
        return bool(elevation.value) if ok else None
    except Exception:
        return None
    finally:
        # O handle do token vaza a cada boot se ninguem fechar.
        if k32 is not None and token is not None and getattr(token, "value", None):
            try:
                k32.CloseHandle(token)
            except Exception:
                pass
