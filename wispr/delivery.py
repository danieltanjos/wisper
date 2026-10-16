# -*- coding: utf-8 -*-
"""Pecas PURAS da entrega de texto, compartilhadas pelas duas plataformas.

`Delivery`, `sanitize()`, `utf16_units()` e a tabela `_CTRL_MAP` sao COPIAS
FIEIS das de `wispr/inject.py` (Windows). O arquivo Windows nao foi tocado no
port para Linux, de proposito: ele nao pode ser importado fora do Windows
(`ctypes.WinDLL` no topo) e nao havia como validar uma refatoracao dele aqui.

Copia que diverge e' o bug classico: o contrato do `Delivery` (os seis
atributos em TODO retorno, docs/CONTRACT.md) passaria a valer so numa
plataforma. Por isso `tests/test_linux.py::DeliveryParityTest` compara o FONTE
das quatro pecas entre os dois arquivos e fica vermelho na primeira diferenca.
Mudou la, muda aqui, no mesmo commit.

Quem usa: `wispr/linux/inject.py`. No Windows ninguem importa este modulo.
"""
from __future__ import annotations

import logging

log = logging.getLogger("wispr.inject")

__all__ = ["Delivery", "sanitize", "utf16_units"]


def utf16_units(text):
    """Vista das code units UTF-16-LE do texto (sem copia por caractere).

    `surrogatepass` de proposito: um surrogate solto (texto que nao passou por
    `sanitize()`) levantaria UnicodeEncodeError no meio da injecao e o ditado
    inteiro sumiria. Melhor perder um glifo.
    """
    return memoryview(text.encode("utf-16-le", "surrogatepass")).cast("H")


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
