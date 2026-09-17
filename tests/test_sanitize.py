# -*- coding: utf-8 -*-
"""Testes da limpeza do texto antes da injecao.

Nenhum teste aqui chama `deliver()`, `type_unicode()` nem toca no clipboard --
`_safety` bloqueia `SendInput`, `OpenClipboard` e companhia no nivel do ctypes,
entao mesmo um engano vira RuntimeError em vez de teclada no jogo do usuario.

O que esta sendo testado e a funcao pura de saneamento. Ela nao esta em
docs/CONTRACT.md (o contrato lista so `deliver/type_unicode/paste_text/...`),
entao o teste procura por ela nos modulos e nomes plausiveis e pula com uma
mensagem explicita se nao achar.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.dirname(_HERE), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import _safety  # noqa: E402  stubs antes de qualquer import de wispr

import unittest

#: Onde a funcao de saneamento pode morar, em ordem de probabilidade.
_MODULES = ("wispr.inject", "wispr.stt", "wispr.text", "wispr.util", "wispr.sanitize")
_NAMES = (
    "sanitize", "sanitize_text", "_sanitize", "_sanitize_text",
    "clean_text", "_clean_text", "scrub_text", "_scrub_text", "_scrub",
    "normalize_text", "_normalize_text",
)

_SANITIZE = None
_ORIGIN = ""
_REASONS = []

for _modname in _MODULES:
    _mod, _why = _safety.load_pure(_modname)
    if _mod is None:
        _REASONS.append(_why)
        continue
    _name, _fn = _safety.find_callable(_mod, _NAMES)
    if _fn is not None:
        _SANITIZE, _ORIGIN = _fn, "%s.%s" % (_modname, _name)
        break
    _REASONS.append("%s existe mas nao expoe nenhum de %s" % (_modname, list(_NAMES[:4])))

_MISSING = (
    "nenhuma funcao pura de saneamento encontrada. Esperado algo como "
    "`def sanitize(text: str) -> str` em wispr/inject.py. Motivos: "
    + " | ".join(_REASONS[:4])
)

_stt, _stt_why = _safety.load_pure("wispr.stt")
_POSTPROCESS = getattr(_stt, "postprocess", None) if _stt else None


class SanitizeTest(unittest.TestCase):

    def setUp(self):
        if _SANITIZE is None:
            self.skipTest(_MISSING)

    def san(self, text):
        out = _SANITIZE(text)
        self.assertIsInstance(out, str, "%s devolveu %r" % (_ORIGIN, type(out)))
        return out

    # --- caracteres de controle ------------------------------------------- #

    def test_nul_and_escape_are_stripped(self):
        out = self.san("a\x00b\x1bc\x07d")
        for bad in ("\x00", "\x1b", "\x07"):
            self.assertNotIn(bad, out)
        self.assertEqual("".join(ch for ch in out if ch.isalpha()), "abcd")

    def test_no_c0_control_survives_except_newline_and_tab(self):
        sujo = "".join(chr(i) for i in range(0x20)) + "texto util"
        out = self.san(sujo)
        sobrou = sorted({ch for ch in out if ord(ch) < 0x20} - {"\n", "\t"})
        self.assertEqual(sobrou, [], "sobraram controles: %r" % sobrou)
        self.assertIn("texto util", out)

    def test_del_and_c1_do_not_break_it(self):
        out = self.san("antes\x7f\x85depois")
        self.assertIn("antes", out)
        self.assertIn("depois", out)
        self.assertNotIn("\x7f", out)

    def test_vertical_tab_and_formfeed_are_gone(self):
        out = self.san("linha\x0bmeio\x0cfim")
        self.assertNotIn("\x0b", out)
        self.assertNotIn("\x0c", out)

    # --- fim de linha ------------------------------------------------------ #

    def test_crlf_is_normalised(self):
        out = self.san("linha um\r\nlinha dois")
        self.assertNotIn("\r", out)
        self.assertIn("linha um", out)
        self.assertIn("linha dois", out)

    def test_lone_cr_is_normalised(self):
        out = self.san("linha um\rlinha dois")
        self.assertNotIn("\r", out)
        self.assertIn("linha dois", out)

    def test_trailing_newline_is_removed(self):
        """CRITICO: a tecla de parada e o Enter.

        Se sobrar um \\n no fim, a injecao manda um Enter extra na janela em
        foco -- o que significa mensagem enviada duas vezes no WhatsApp, ou o
        Enter voltando para o nosso proprio hook.
        """
        for sujo in ("texto\n", "texto\r\n", "texto\n\n\n", "texto\r", "texto \n"):
            with self.subTest(sujo=sujo):
                out = self.san(sujo)
                self.assertFalse(out.endswith("\n"), "sobrou \\n em %r" % out)
                self.assertFalse(out.endswith("\r"), "sobrou \\r em %r" % out)
                self.assertIn("texto", out)

    def test_only_newlines_collapses_to_nothing_meaningful(self):
        self.assertEqual(self.san("\r\n\r\n").strip(), "")

    # --- unicode ----------------------------------------------------------- #

    def test_accents_are_preserved(self):
        frase = "A ação do coração é inversamente proporcional à pressa, não é?"
        self.assertEqual(self.san(frase), frase)

    def test_every_ptbr_diacritic_survives(self):
        frase = "áàâãéêíóôõúüç ÁÀÂÃÉÊÍÓÔÕÚÜÇ"
        out = self.san(frase)
        for ch in frase.replace(" ", ""):
            self.assertIn(ch, out, "perdeu o caractere %r" % ch)

    def test_astral_chars_survive(self):
        """Fora do BMP viram par surrogate no SendInput; nao podem ser cortados."""
        frase = "deploy feito 🚀 tudo certo 👍"
        out = self.san(frase)
        self.assertIn("🚀", out)
        self.assertIn("👍", out)
        self.assertEqual(len([c for c in out if ord(c) > 0xFFFF]), 2)

    def test_result_is_encodable_as_utf16(self):
        """KEYEVENTF_UNICODE manda UTF-16LE; string invalida quebraria o envio."""
        out = self.san("emoji 🚀 acento ção \x00 controle")
        self.assertEqual(out.encode("utf-16-le").decode("utf-16-le"), out)

    def test_lone_surrogate_does_not_crash(self):
        out = self.san("antes \ud83d depois")
        self.assertIn("antes", out)
        self.assertIn("depois", out)
        out.encode("utf-16-le", errors="surrogatepass")  # nao pode estourar

    # --- invariantes ------------------------------------------------------- #

    def test_clean_text_is_untouched(self):
        frase = "roda o docker compose up e depois abre o endpoint"
        self.assertEqual(self.san(frase), frase)

    def test_is_idempotent(self):
        for value in ("texto\r\n", "a\x00b", "ação 🚀\n", "   ", ""):
            with self.subTest(value=value):
                once = self.san(value)
                self.assertEqual(self.san(once), once)

    def test_empty_string(self):
        self.assertEqual(self.san(""), "")


class PipelineNewlineTest(unittest.TestCase):
    """Rede de seguranca: qualquer funcao pura do caminho do texto vale.

    Mesmo que a funcao de saneamento ainda nao exista, o texto que chega na
    injecao nunca pode terminar em quebra de linha.
    """

    def setUp(self):
        self.fns = [(n, f) for n, f in (("sanitize", _SANITIZE),
                                        ("postprocess", _POSTPROCESS)) if f]
        if not self.fns:
            self.skipTest("nem sanitize nem postprocess disponiveis: %s" % _MISSING)

    def _call(self, fn, name, text):
        return fn(text) if name == "sanitize" else fn(text, [])

    def test_output_never_ends_with_a_line_break(self):
        entradas = ["texto\n", "texto\r\n", "linha\nlinha\n", "texto\n \n"]
        for name, fn in self.fns:
            for sujo in entradas:
                with self.subTest(fn=name, sujo=sujo):
                    out = self._call(fn, name, sujo)
                    self.assertFalse(out.endswith(("\n", "\r")),
                                     "%s devolveu %r" % (name, out))

    def test_output_never_contains_a_carriage_return(self):
        for name, fn in self.fns:
            with self.subTest(fn=name):
                self.assertNotIn("\r", self._call(fn, name, "a\r\nb\rc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
