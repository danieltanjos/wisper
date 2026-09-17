# -*- coding: utf-8 -*-
"""Blindagem da suite de testes. Importar este modulo e a PRIMEIRA coisa que todo
test_*.py faz, antes de qualquer `import wispr`.

Duas garantias, nessa ordem:

1. Os modulos pesados entram em `sys.modules` como stubs. Importar `sounddevice`
   de verdade roda `Pa_Initialize`, que sobe o backend WASAPI e chama
   `CoInitializeEx` -- ou seja, o simples import ja mexe no subsistema de audio.
   O mesmo vale para `faster_whisper` (CUDA), `tkinter` (janela), `pystray`
   (icone) e `winsound` (abre endpoint de saida e apita no fone de quem esta
   jogando). Nada disso pode acontecer com a maquina em uso.
2. As chamadas Win32 destrutivas viram bombas de efeito nulo. Se algum modulo
   de `wispr/` chamar `SendInput`, `SetWindowsHookExW`, `OpenClipboard` ou
   `GetClipboardData` durante um teste, o teste explode com RuntimeError em vez
   de sequestrar o teclado ou ler o clipboard do usuario.
   Atribuir `.argtypes`/`.restype` continua funcionando, porque isso e feito no
   nivel do modulo e nao chega a chamar nada.

Ver docs/ARCHITECTURE.md secoes 3, 4 e 5.
"""
from __future__ import annotations

import ctypes
import importlib
import logging
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)

for _p in (ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------- #
# 1. stubs de modulo
# --------------------------------------------------------------------------- #

class _AnyMeta(type):
    """Metaclasse do coringa: a propria classe aceita atributo e indexacao."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _any(name)

    def __getitem__(cls, item):
        return cls

    def __iter__(cls):
        return iter(())

    def __repr__(cls):
        return "<stub %s>" % cls.__name__


class Any(Exception, metaclass=_AnyMeta):
    """Coringa que aguenta tudo que um modulo faz no nivel do modulo.

    E uma classe, e nao um objeto, de proposito: `class Client(COMObject)` no
    topo de audio.py precisa de uma base herdavel, senao o import quebraria e o
    teste sumiria num skip silencioso.

    Herda de Exception porque `except sd.PortAudioError:` levanta
    `TypeError: catching classes that do not inherit from BaseException` quando
    o nome vem de um stub comum -- e isso so aparece na hora em que uma excecao
    passa por ali, ou seja, exatamente no caminho de erro que o teste queria
    cobrir. Nenhum modulo de wispr/ faz isso hoje; e seguro de graca.
    """

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _any(name)

    def __call__(self, *a, **k):
        return _any("call")()

    def __getitem__(self, item):
        return _any("item")()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __repr__(self):
        return "<stub instance>"


def _any(name: str):
    """Fabrica uma subclasse nova de Any com o nome pedido."""
    return _AnyMeta(str(name), (Any,), {})


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _any("%s.%s" % (self.__name__, name))


#: Modulos que NAO podem ser importados de verdade nesta suite.
#:
#: `comtypes` NAO esta aqui de proposito. Ele so faz `CoInitializeEx` e definir
#: structs -- nao encosta em hardware -- e o proprio `wispr/__init__.py` importa
#: ele no import do pacote. Alem disso as interfaces COM de audio.py sao
#: declaradas com `ctypes.POINTER(IUnknown)`, que exige um tipo ctypes de
#: verdade: com stub o modulo inteiro falharia no import e os testes de DSP e de
#: ring sumiriam num skip.
HEAVY = (
    # audio -- importar sounddevice ja roda Pa_Initialize
    "sounddevice", "_sounddevice", "soundfile", "_soundfile",
    "pycaw", "pycaw.pycaw", "pycaw.constants", "pycaw.utils",
    "webrtcvad",
    # `winsound` e importado no topo de wispr/overlay.py e usado por app.py para o
    # ping de inicio/fim. PlaySound/Beep abrem um endpoint de SAIDA e soam alto no
    # fone de quem esta jogando: stub, sempre.
    "winsound",
    # ASR / GPU
    "faster_whisper", "ctranslate2", "torch", "huggingface_hub", "transformers",
    # UI
    "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox",
    "pystray", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    # injecao de teclado de terceiros
    "keyboard", "pyautogui", "pyperclip",
    "win32api", "win32con", "win32gui", "win32clipboard",
    # rede (o caminho groq nunca e exercitado, mas o import precisa passar)
    "requests",
)

_PACKAGES = {"pycaw", "PIL", "tkinter"}

stubbed: list[str] = []


def install_stubs() -> list[str]:
    """Idempotente: poe os stubs em sys.modules e devolve o que foi stubado.

    A varredura roda inteira em toda chamada, sem atalho pelo `stubbed`: se algum
    teste apagar uma entrada de sys.modules, a proxima chamada repoe o stub em vez
    de deixar o modulo de verdade entrar pela porta dos fundos.
    """
    for name in HEAVY:
        if name in sys.modules:
            continue
        mod = _StubModule(name)
        mod.__doc__ = "stub offline instalado por tests/_safety.py"
        if name in _PACKAGES:
            mod.__path__ = []  # marca como pacote, senao `import x.y` falha
        sys.modules[name] = mod
        if name not in stubbed:
            stubbed.append(name)
    # amarra cada submodulo ao pai, para `from PIL import Image` funcionar.
    # So mexe em pai que tambem e stub: se o modulo de verdade ja estava
    # carregado, nao e nosso lugar remendar ele.
    for name in HEAVY:
        if "." in name:
            parent, _, leaf = name.rpartition(".")
            pmod = sys.modules.get(parent)
            if isinstance(pmod, _StubModule) and isinstance(sys.modules.get(name), _StubModule):
                setattr(pmod, leaf, sys.modules[name])
    return stubbed


# --------------------------------------------------------------------------- #
# 2. trava das chamadas Win32 perigosas
# --------------------------------------------------------------------------- #

#: Nomes que, se chamados, mexeriam no teclado, no mouse, no clipboard ou na tela
#: do usuario. A suite inteira tem que passar sem tocar em nenhum deles.
BLOCKED_WIN32 = frozenset({
    # teclado / mouse
    "SendInput", "keybd_event", "mouse_event", "BlockInput",
    "SetCursorPos", "ClipCursor",
    "SetWindowsHookExW", "SetWindowsHookExA", "SetWinEventHook",
    # RegisterHotKey nao esta em wispr/hotkey.py (ele so usa WH_KEYBOARD_LL), mas
    # registrar um chord global roubaria a tecla do jogo do usuario na hora.
    "RegisterHotKey",
    # clipboard: ler tambem e proibido, nao so escrever. CloseClipboard fica de
    # fora de proposito -- se algum caminho conseguir abrir, tem que poder fechar.
    "OpenClipboard", "SetClipboardData", "EmptyClipboard",
    "GetClipboardData", "EnumClipboardFormats",
    "MessageBoxW", "MessageBoxA",
    "CreateWindowExW", "CreateWindowExA", "SetForegroundWindow",
    "CoCreateInstance",
})


class _BlockedFunc:
    """Substituto de um symbol de DLL bloqueado.

    Aceita `.argtypes = ...` e `.restype = ...` em silencio -- modulos declaram
    isso no topo do arquivo e isso e inofensivo -- mas estoura se alguem chamar.
    """

    def __init__(self, name: str):
        self.__dict__["_blocked_name"] = name

    def __setattr__(self, key, value):
        self.__dict__[key] = value

    def __call__(self, *a, **k):
        raise RuntimeError(
            "wisper tests: chamada a %s bloqueada. A suite tem que rodar sem "
            "tocar em teclado, clipboard, audio ou tela." % self.__dict__["_blocked_name"]
        )

    def __repr__(self):
        return "<blocked %s>" % self.__dict__["_blocked_name"]


_win32_blocked = False


def block_win32() -> bool:
    """Envolve ctypes.CDLL.__getattr__ (WinDLL herda dele) com a lista negra."""
    global _win32_blocked
    if _win32_blocked:
        return True
    original = ctypes.CDLL.__getattr__

    def guarded(self, name):
        if name in BLOCKED_WIN32:
            return _BlockedFunc(name)
        return original(self, name)

    ctypes.CDLL.__getattr__ = guarded
    _win32_blocked = True
    return True


# --------------------------------------------------------------------------- #
# 3. utilitarios para os testes
# --------------------------------------------------------------------------- #

def load_pure(modname: str):
    """Importa um modulo de wispr com os stubs no lugar.

    Devolve (modulo, None) ou (None, motivo). Captura Exception inteira de
    proposito: os modulos ainda podem estar sendo escritos em paralelo, e um
    ImportError, um AttributeError num stub ou um erro de sintaxe tem que virar
    skip com explicacao, nunca um erro vermelho sem contexto.
    """
    install_stubs()
    block_win32()
    try:
        return importlib.import_module(modname), None
    except Exception as exc:  # noqa: BLE001 - e exatamente o que queremos aqui
        return None, "%s indisponivel (%s: %s)" % (modname, type(exc).__name__, exc)


def find_callable(mod, names):
    """Primeiro atributo chamavel de `mod` cujo nome esta em `names`."""
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return name, fn
    return None, None


def find_sequence(mod, names):
    """Primeira constante iteravel de strings de `mod` cujo nome esta em `names`."""
    for name in names:
        value = getattr(mod, name, None)
        if isinstance(value, (list, tuple, set, frozenset)) and value:
            if all(isinstance(v, str) for v in value):
                return name, list(value)
    return None, None


def has_module(name: str) -> bool:
    """True se o pacote esta instalado, SEM importar (usa o finder)."""
    if name in sys.modules:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def null_logger(name: str = "wisper.test") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def quiet_logging() -> None:
    """Sem isso, um `log.warning` de dentro de wispr cai no lastResort do logging
    e polui a saida do unittest com linhas soltas em stderr.

    Duas arvores: os modulos usam `logging.getLogger(__name__)` ('wispr.audio')
    e o logging_setup reescreve para o nome do app ('wisper.audio').
    """
    for name in ("wispr", "wisper"):
        root = logging.getLogger(name)
        if not root.handlers:
            root.addHandler(logging.NullHandler())
        root.propagate = False


# Efeito colateral no import, de proposito: quem importar _safety ja esta seguro.
install_stubs()
block_win32()
quiet_logging()
