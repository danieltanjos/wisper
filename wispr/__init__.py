# -*- coding: utf-8 -*-
"""Pacote wisper.

Este arquivo existe por um motivo so: fixar a ordem global de import do
processo. E a coisa mais fragil do app inteiro.

Importar `sounddevice` roda `Pa_Initialize()`, e o backend WASAPI do PortAudio
chama `CoInitializeEx(COINIT_APARTMENTTHREADED)`. Depois disso, importar
`comtypes` levanta RPC_E_CHANGED_MODE (-2147417850) e toda a resolucao do
microfone por COM (IMMDeviceEnumerator / IMMNotificationClient) morre junto.
Medido nesta maquina, nao e teoria. Ver docs/ARCHITECTURE.md secao 4.

Por isso: `sys.coinit_flags = 0` (MTA) e `import comtypes` acontecem aqui, no
import do pacote, antes que qualquer submodulo tenha chance de puxar audio.
O `comtypes` le `sys.coinit_flags` e chama `CoInitializeEx` no import dele
mesmo, entao setar a flag depois disso nao teria efeito nenhum.

Importar o pacote precisa continuar barato: nada de `wispr.audio` ou
`wispr.stt` aqui dentro.

COM_READY / COM_ERROR sao rebindados por `ensure_import_order()`. Leia sempre
como atributo (`wispr.COM_READY`), nunca com `from wispr import COM_READY`, ou
voce congela o valor de antes.
"""
from __future__ import annotations

import sys

__version__ = "1.0.0"

__all__ = ["__version__", "COM_READY", "COM_ERROR", "RPC_E_CHANGED_MODE",
           "ensure_import_order"]

#: HRESULT de quem tentou mudar o apartment model depois do PortAudio.
RPC_E_CHANGED_MODE = -2147417850
#: O mesmo HRESULT visto como DWORD: dependendo do caminho, o ctypes entrega
#: `winerror` sem sinal (0x80010106), e ai a comparacao com o valor negativo
#: falha em silencio.
_RPC_E_CHANGED_MODE_U = RPC_E_CHANGED_MODE & 0xFFFFFFFF

#: True quando `comtypes` esta importado e utilizavel neste processo.
COM_READY: bool = False

#: Motivo da falha, ou aviso de ordem. Pode estar preenchido mesmo com
#: COM_READY True - nesse caso e um aviso (a ordem nao pode ser garantida),
#: nao uma falha. Quem depende de COM deve olhar COM_READY primeiro.
COM_ERROR: str | None = None

_ordered = False


def _hresult_of(exc: BaseException) -> int | None:
    """HRESULT de uma excecao de COM, venha ela como OSError do ctypes
    (`winerror`), como `comtypes.COMError` (`hresult`) ou so em args[0]."""
    for attr in ("winerror", "hresult"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return args[0]
    return None


def _is_changed_mode(exc: BaseException) -> bool:
    return _hresult_of(exc) in (RPC_E_CHANGED_MODE, _RPC_E_CHANGED_MODE_U)


def ensure_import_order() -> None:
    """Poe o processo em MTA e puxa o `comtypes` antes de qualquer coisa que
    possa importar `sounddevice`. Idempotente: a segunda chamada nao faz nada.

    Nunca levanta excecao. Numa maquina sem `comtypes` (dev box antes do
    setup.ps1, ou um simples syntax check) o pacote continua importavel e a
    falha fica registrada em COM_READY / COM_ERROR para quem quiser reagir.
    """
    global COM_READY, COM_ERROR, _ordered
    if _ordered:
        return
    _ordered = True

    problems: list[str] = []

    # Se o PortAudio ja rodou, o apartment desta thread ja esta em STA e nao ha
    # mais o que consertar aqui. So registrar, porque o sintoma aparece longe.
    if "sounddevice" in sys.modules:
        problems.append(
            "sounddevice was imported before wispr: the COM apartment is "
            "probably already STA"
        )

    if "comtypes" in sys.modules:
        problems.append(
            "comtypes was already imported before wispr: sys.coinit_flags "
            "could not be enforced"
        )
    else:
        try:
            sys.coinit_flags = 0  # COINIT_MULTITHREADED
        except Exception as exc:  # pragma: no cover - nao deveria acontecer
            problems.append(f"could not set sys.coinit_flags: {exc!r}")

    try:
        import comtypes  # noqa: F401  (importado so pelo efeito colateral)
    except ImportError as exc:
        COM_READY = False
        problems.append(f"comtypes is not installed: {exc}")
    except Exception as exc:
        # RPC_E_CHANGED_MODE chega como OSError do ctypes (restype HRESULT),
        # mas versoes/caminhos diferentes do comtypes embrulham em COMError.
        # Por isso o HRESULT e extraido, nao o tipo da excecao.
        COM_READY = False
        hint = " (RPC_E_CHANGED_MODE: PortAudio/COM came first)" if _is_changed_mode(exc) else ""
        problems.append(f"comtypes import failed{hint}: {exc!r}")
    else:
        COM_READY = True

    COM_ERROR = "; ".join(problems) if problems else None


try:
    ensure_import_order()
except Exception as _exc:  # pragma: no cover - cinto e suspensorio
    # Nada aqui pode impedir `import wispr`: sem o pacote nao ha log, nao ha
    # bandeja e o usuario nao ve absolutamente nada acontecer.
    COM_READY = False
    COM_ERROR = f"ensure_import_order crashed: {_exc!r}"
