# -*- coding: utf-8 -*-
"""Testes de wispr.stt.postprocess -- a unica parte pura do modulo de ASR.

Nada aqui carrega modelo, toca a GPU ou aloca VRAM: `_safety` troca
`faster_whisper` e `ctranslate2` por stubs antes do import, entao so o codigo
Python de wispr/stt.py roda.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.dirname(_HERE), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import _safety  # noqa: E402  stubs antes de qualquer import de wispr

import inspect
import unittest

from wispr import config

_stt, _why = _safety.load_pure("wispr.stt")
postprocess = getattr(_stt, "postprocess", None) if _stt else None

#: A blacklist so pode disparar quando o Engine avisa que a transcricao veio de
#: UM segmento curto -- por keyword, fora do contrato de duas posicoes. Se essa
#: costura nao existir, os testes caem para a chamada simples.
_FLAG = "single_short_segment"
try:
    _HAS_FLAG = bool(postprocess) and _FLAG in inspect.signature(postprocess).parameters
except (TypeError, ValueError):  # pragma: no cover - builtin sem assinatura
    _HAS_FLAG = False

#: Nomes plausiveis para a lista negra de alucinacao exportada por stt.py.
_BLACKLIST_NAMES = (
    "HALLUCINATIONS", "HALLUCINATION_BLACKLIST", "HALLUCINATION", "BLACKLIST",
    "JUNK", "NOISE_PHRASES",
    "_HALLUCINATIONS", "_HALLUCINATION_BLACKLIST", "_BLACKLIST", "_JUNK",
)

#: Alucinacoes canonicas do Whisper em pt-BR (legenda de YouTube no treino). So
#: entram em cena se stt.py renomear a constante dele; por isso sao as duas
#: frases que qualquer blacklist pt-BR tem, e nao um chute mais criativo.
_CANONICAL = [
    "Legendas pela comunidade Amara.org",
    "Amara.org",
]


def setUpModule():
    if postprocess is None:
        raise unittest.SkipTest(_why or "wispr.stt nao expoe postprocess()")


_SENTINEL = object()

#: Se stt.py guardar regex em vez de frase literal, nao da para usar a lista
#: dele como entrada de teste -- caimos na canonica.
_REGEX_CHARS = set(r"^$\[]()|*+?{}")


class _Base(unittest.TestCase):

    def setUp(self):
        self.fixups = config.DEFAULTS["fixups"]

    def pp(self, text, fixups=_SENTINEL):
        """A chamada do CONTRACT.md, de duas posicoes. Nunca censura nada."""
        return postprocess(text, self.fixups if fixups is _SENTINEL else fixups)

    def pp_short(self, text):
        """A chamada que o Engine faz quando veio um unico segmento curto."""
        if _HAS_FLAG:
            return postprocess(text, self.fixups, **{_FLAG: True})
        return postprocess(text, self.fixups)


class FixupsTest(_Base):
    """O mapa de fixups existe para o que o initial_prompt nao conserta."""

    def test_brand_becomes_branch(self):
        out = self.pp("criei uma brand nova pro hotfix")
        self.assertIn("branch", out)
        self.assertNotIn("brand ", out)

    def test_capitalised_brand_becomes_branch(self):
        self.assertIn("branch", self.pp("fiz merge na Brand principal"))

    def test_docker_campus_up_becomes_docker_compose_up(self):
        self.assertIn("docker compose up", self.pp("roda docker campus up ai"))

    def test_docker_campus_app_becomes_docker_compose_up(self):
        self.assertIn("docker compose up", self.pp("Docker Campus App e espera subir"))

    def test_docker_campus_up_titlecase(self):
        self.assertIn("docker compose up", self.pp("Docker Campus up no servidor"))

    def test_middleware_variants(self):
        for wrong in ("mid-leware", "midware"):
            with self.subTest(wrong=wrong):
                out = self.pp("o %s de autenticacao quebrou" % wrong)
                self.assertIn("middleware", out)
                self.assertNotIn(wrong, out)

    def test_fixups_do_not_eat_longer_words(self):
        """' brand ' tem espacos de proposito: 'brandy' nao pode ser tocado."""
        self.assertIn("brandy", self.pp("comprei uma brandy no mercado"))

    def test_several_fixups_in_one_pass(self):
        out = self.pp("na brand de teste o midware caiu")
        self.assertIn("branch", out)
        self.assertIn("middleware", out)

    def test_text_without_any_match_is_untouched(self):
        frase = "esse commit precisa de um rebase antes do merge"
        self.assertEqual(self.pp(frase), frase)

    def test_empty_fixups_is_not_an_error(self):
        self.assertEqual(self.pp("commit limpo", fixups=[]), "commit limpo")

    def test_none_fixups_is_not_an_error(self):
        self.assertEqual(self.pp("commit limpo", None), "commit limpo")

    def test_tuple_of_tuples_is_accepted(self):
        out = postprocess("a brand nova", ((" brand ", " branch "),))
        self.assertIn("branch", out)

    def test_custom_fixup_wins(self):
        out = postprocess("chamei o cliente Acmi hoje", [["Acmi", "ACME"]])
        self.assertIn("ACME", out)
        self.assertNotIn("Acmi", out)


class WhitespaceTest(_Base):

    def test_runs_of_spaces_collapse(self):
        self.assertEqual(self.pp("texto   com    muito  espaco"),
                         "texto com muito espaco")

    def test_leading_and_trailing_space_go_away(self):
        self.assertEqual(self.pp("   tudo certo   "), "tudo certo")

    def test_newlines_and_tabs_collapse_to_single_spaces(self):
        out = self.pp("uma linha\n\noutra linha\tcom tab")
        self.assertEqual(out, "uma linha outra linha com tab")

    def test_no_double_space_survives(self):
        self.assertNotIn("  ", self.pp("  a  b   c  "))

    def test_nbsp_and_exotic_space_do_not_break_it(self):
        """Escrito com escape de proposito: espaco invisivel no fonte de um teste
        e armadilha. A versao anterior deste caso comparava dois NBSP achando que
        comparava dois espacos, e por isso nao testava nada.

        `\\s` de padrao str no Python 3 casa NBSP e espaco fino, entao o colapso
        do postprocess resolve os dois sem tratamento especial.
        """
        out = self.pp("antes\xa0depois\u2009ainda")
        self.assertEqual(out, "antes depois ainda")
        self.assertNotIn("\xa0", out)
        self.assertNotIn("\u2009", out)

    def test_empty_input(self):
        self.assertEqual(self.pp(""), "")

    def test_whitespace_only_input(self):
        self.assertEqual(self.pp("   \n\t  "), "")

    def test_accents_survive_the_collapse(self):
        frase = "a ação do coração é inversamente proporcional à pressa"
        self.assertEqual(self.pp("  " + frase + "  "), frase)

    def test_fixup_still_fires_after_collapse(self):
        """' brand ' depende de espaco simples; o colapso tem que vir antes."""
        self.assertIn("branch", self.pp("uma   brand   nova"))


class HallucinationTest(_Base):
    """A lista negra so pode disparar num segmento curto e sozinho.

    Whisper alucina legenda de YouTube quando o audio e so silencio ou ruido.
    Filtrar isso e barato; comer fala de verdade e inaceitavel.
    """

    def setUp(self):
        super().setUp()
        name, values = _safety.find_sequence(_stt, _BLACKLIST_NAMES)
        if values:
            # frozenset nao tem ordem: sem o sorted, o teste varia entre rodadas.
            values = sorted(v for v in values if not (set(v) & _REGEX_CHARS))
        if values:
            self.blacklist_name, self.blacklist = name, values
        else:
            self.blacklist_name, self.blacklist = None, list(_CANONICAL)

    def _origem(self):
        return self.blacklist_name or "lista canonica do teste"

    def test_a_lone_hallucination_is_dropped(self):
        for phrase in self.blacklist[:12]:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    self.pp_short(phrase), "",
                    "a frase %r, sozinha num segmento curto, deveria virar string "
                    "vazia (origem: %s)" % (phrase, self._origem()),
                )

    def test_the_two_argument_contract_call_never_censors(self):
        """`postprocess(text, fixups)` e a chamada do CONTRACT.md: nunca apaga nada."""
        if not _HAS_FLAG:
            self.skipTest("postprocess nao separa o caso de segmento curto")
        for phrase in self.blacklist[:12]:
            with self.subTest(phrase=phrase):
                self.assertNotEqual(self.pp(phrase), "")

    def test_hallucination_with_stray_whitespace_is_still_dropped(self):
        phrase = self.blacklist[0]
        self.assertEqual(self.pp_short("  " + phrase + "  \n"), "")

    def test_trailing_punctuation_does_not_save_a_hallucination(self):
        phrase = self.blacklist[0]
        for suffix in (".", "...", "!", "?", " ."):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.pp_short(phrase + suffix), "")

    def test_ellipsis_only_audio_is_dropped(self):
        """Audio quase vazio costuma voltar so como reticencias."""
        for junk in ("...", "…", " . . . "):
            with self.subTest(junk=junk):
                self.assertEqual(self.pp_short(junk), "")

    def test_hallucination_inside_real_speech_is_not_dropped(self):
        phrase = self.blacklist[0]
        frase = ("entao eu falei pro cliente que %s era so uma piada interna "
                 "e a gente seguiu com o deploy normalmente" % phrase)
        out = self.pp_short(frase)
        self.assertNotEqual(out, "")
        self.assertIn("cliente", out)
        self.assertIn("deploy", out)

    def test_hallucination_as_a_prefix_of_real_speech_is_not_dropped(self):
        phrase = self.blacklist[0]
        out = self.pp_short(phrase + " mas o que eu queria mesmo era falar do endpoint "
                                     "de autenticacao que ainda esta quebrado em producao")
        self.assertNotEqual(out, "")
        self.assertIn("endpoint", out)

    def test_real_speech_is_never_emptied(self):
        frases = [
            "abre o terminal e roda o teste de novo",
            "preciso subir esse endpoint antes das seis da tarde",
            "manda no grupo que o deploy ja foi",
            "o pull request esta esperando revisao desde ontem",
            "não",
            "sim, pode mandar",
            "cancela",
            "obrigado pela ajuda com o merge de ontem, ficou perfeito",
        ]
        conhecidas = {b.strip().lower() for b in self.blacklist}
        for frase in frases:
            if frase.strip().lower() in conhecidas:
                continue          # essa entrada e alucinacao declarada, nao fala real
            with self.subTest(frase=frase):
                # pp_short e o caminho mais severo: mesmo ali, fala real fica.
                self.assertNotEqual(self.pp_short(frase), "")

    def test_long_transcripts_are_never_emptied(self):
        """Regra de ouro: transcricao longa nunca vira vazio, aconteca o que acontecer."""
        longa = ("hoje eu quero refatorar o middleware de autenticacao porque ele "
                 "esta chamando o banco tres vezes por requisicao e isso derruba "
                 "o tempo de resposta em producao toda vez que da pico")
        self.assertNotEqual(self.pp_short(longa), "")
        self.assertIn("middleware", self.pp_short(longa))

    def test_two_hallucinations_glued_together_are_still_short_junk(self):
        if len(self.blacklist) < 2:
            self.skipTest("lista negra tem menos de duas entradas")
        # Nao exigimos que vire vazio -- exigimos que nao estoure.
        out = self.pp_short(self.blacklist[0] + " " + self.blacklist[1])
        self.assertIsInstance(out, str)


class ContractTest(_Base):
    """Invariantes que o app.py assume sem conferir."""

    def test_always_returns_a_string(self):
        for value in ("", " ", "ok", "a brand nova", "\n\n"):
            with self.subTest(value=value):
                self.assertIsInstance(self.pp(value), str)

    def test_is_idempotent(self):
        for value in ("uma brand nova   aqui", "  espaco  ", "docker campus up"):
            with self.subTest(value=value):
                once = self.pp(value)
                self.assertEqual(self.pp(once), once)

    def test_does_not_add_a_trailing_newline(self):
        """O stop key e Enter: um \\n no fim do texto dispararia a tecla de novo."""
        for value in ("texto\n", "texto\r\n", "texto", "texto\n\n\n"):
            with self.subTest(value=value):
                out = self.pp(value)
                self.assertFalse(out.endswith("\n"), "sobrou \\n no fim de %r" % out)
                self.assertFalse(out.endswith("\r"), "sobrou \\r no fim de %r" % out)

    def test_does_not_mutate_the_fixups_list(self):
        antes = [list(p) for p in self.fixups]
        self.pp("uma brand nova")
        self.assertEqual([list(p) for p in self.fixups], antes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
