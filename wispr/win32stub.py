# -*- coding: utf-8 -*-
"""Substituto das DLLs Win32 fora do Windows.

`wispr/app.py` e `wispr/overlay.py` sao os mesmos arquivos nas duas
plataformas, e os dois declaram no topo `dll.Funcao.argtypes = ...` /
`.restype = ...`. No Linux nao existe `ctypes.WinDLL`. Em vez de indentar o bloco
Win32 inteiro sob um `if`, `dll("user32")` devolve um objeto que ACEITA essas
atribuicoes e estoura com OSError se alguem CHAMAR uma funcao. Os caminhos Linux
nunca chamam (cada um tem seu ramo `sys.platform`); quem chama por engano cai no
`except` que ja envolve toda chamada Win32 desses arquivos, e o resultado e' o
mesmo fallback de quando a API falha no Windows.

No Windows `dll()` e' exatamente `ctypes.WinDLL(name, use_last_error=True)`.
"""
from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"


class _Fn:
    """Funcao Win32 inexistente: aceita argtypes/restype, estoura ao ser chamada."""

    def __init__(self, name: str):
        self.__dict__["_name"] = name

    def __setattr__(self, key, value):
        self.__dict__[key] = value

    def __call__(self, *a, **k):
        raise OSError("%s nao existe fora do Windows" % self.__dict__["_name"])

    def __repr__(self):
        return "<win32stub %s>" % self.__dict__["_name"]


class NoWin32:
    """DLL Win32 inexistente."""

    def __init__(self, name: str):
        self._dll = name

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fn = _Fn("%s.%s" % (self._dll, name))
        # Guardar: `dll.X.restype = ...` seguido de `dll.X(...)` tem que falar
        # do MESMO objeto, como no ctypes de verdade.
        self.__dict__[name] = fn
        return fn


def dll(name: str):
    """`ctypes.WinDLL(name, use_last_error=True)` no Windows; `NoWin32` fora dele."""
    if IS_WINDOWS:
        return ctypes.WinDLL(name, use_last_error=True)
    return NoWin32(name)
