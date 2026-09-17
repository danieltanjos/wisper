# -*- coding: utf-8 -*-
"""Testes de wispr/config.py. Puro sistema de arquivos, nada de audio ou GPU."""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.dirname(_HERE), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import _safety  # noqa: E402,F401  stubs antes de qualquer import de wispr

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from wispr import config


class DefaultsTest(unittest.TestCase):
    """Carregar sem arquivo nenhum tem que devolver exatamente os defaults."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wisper_cfg_"))
        self.path = self.tmp / "nao_existe.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_gives_defaults(self):
        cfg = config.load(self.path)
        self.assertEqual(dict(cfg), config.DEFAULTS)

    def test_every_default_key_is_present(self):
        cfg = config.load(self.path)
        for key in config.DEFAULTS:
            self.assertIn(key, cfg, "chave %r sumiu do load()" % key)

    def test_attribute_access(self):
        cfg = config.load(self.path)
        self.assertEqual(cfg.hotkey, cfg["hotkey"])
        self.assertEqual(cfg.engine, "local")
        self.assertEqual(cfg.language, "pt")

    def test_attribute_write_reaches_the_dict(self):
        cfg = config.load(self.path)
        cfg.hotkey = "ctrl+alt+a"
        self.assertEqual(cfg["hotkey"], "ctrl+alt+a")

    def test_unknown_attribute_raises_attribute_error(self):
        cfg = config.load(self.path)
        with self.assertRaises(AttributeError):
            cfg.chave_que_nao_existe  # noqa: B018

    def test_load_does_not_alias_the_defaults(self):
        """load() faz deepcopy: mexer no cfg nao pode contaminar DEFAULTS."""
        original = copy.deepcopy(config.DEFAULTS["fixups"])
        cfg = config.load(self.path)
        cfg["fixups"].append(["xyzzy", "plugh"])
        cfg["initial_prompt"] = "outra coisa"
        self.assertEqual(config.DEFAULTS["fixups"], original)
        self.assertNotEqual(config.DEFAULTS["initial_prompt"], "outra coisa")

    def test_two_loads_are_independent(self):
        a = config.load(self.path)
        b = config.load(self.path)
        a["fixups"].append(["a", "b"])
        self.assertNotEqual(a["fixups"], b["fixups"])


class ContractedValuesTest(unittest.TestCase):
    """Valores que outros modulos e o README dependem de encontrar."""

    def test_paths_are_inside_the_project(self):
        root = config.ROOT
        for p in (config.MODELS_DIR, config.LOG_DIR, config.ASSETS_DIR, config.CONFIG_PATH):
            self.assertEqual(Path(p).parent, root, "%s saiu da raiz do projeto" % p)

    def test_hf_home_is_exported_on_import(self):
        # huggingface_hub le HF_HOME no import dele; config tem que setar antes.
        # E `setdefault`, entao quem ja tinha HF_HOME no ambiente continua mandando
        # (e um HF_HOME global e comum em maquina de ML). Isso e feature, nao bug:
        # o teste distingue os dois casos em vez de ficar vermelho sem culpado.
        atual = os.environ.get("HF_HOME")
        self.assertTrue(atual, "config nao exportou HF_HOME no import")
        if atual != str(config.MODELS_DIR):
            self.skipTest("HF_HOME ja vinha do ambiente (%s); config.py usa "
                          "setdefault e respeita isso de proposito" % atual)

    def test_mutex_name_is_local_scoped(self):
        self.assertTrue(config.MUTEX_NAME.startswith("Local\\"))

    def test_prompt_has_no_accents(self):
        """Medido: prompt sem acento deu WER 8,5% contra 9,8% do acentuado."""
        prompt = config.DEFAULTS["initial_prompt"]
        self.assertTrue(prompt.isascii(), "o initial_prompt padrao ganhou acentos")

    def test_prompt_is_short_enough_to_survive_truncation(self):
        """faster-whisper corta o prompt em 224 tokens; longo fica pior que nenhum."""
        palavras = len(config.DEFAULTS["initial_prompt"].split())
        self.assertLess(palavras, 160, "initial_prompt longo demais, vai ser truncado")

    def test_fixups_are_pairs_of_strings(self):
        for pair in config.DEFAULTS["fixups"]:
            self.assertEqual(len(pair), 2, "fixup %r nao e um par" % (pair,))
            self.assertTrue(all(isinstance(s, str) for s in pair))

    def test_silence_gate_is_tiny_but_not_zero(self):
        """O gate existe por causa do silencio digital do headset sem fio."""
        self.assertGreater(config.DEFAULTS["silence_rms"], 0.0)
        self.assertLess(config.DEFAULTS["silence_rms"], 1e-3)

    def test_capture_rate_is_48k(self):
        """WASAPI compartilhado neste endpoint so aceita 48000 Hz."""
        self.assertEqual(config.DEFAULTS["capture_sr"], 48000)
        self.assertEqual(config.DEFAULTS["target_sr"], 16000)


class RoundTripTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wisper_cfg_"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_keeps_the_change(self):
        cfg = config.load(self.path)
        cfg["hotkey"] = "ctrl+alt+a"
        cfg["inject_mode"] = "paste"
        config.save(cfg, self.path)

        again = config.load(self.path)
        self.assertEqual(again["hotkey"], "ctrl+alt+a")
        self.assertEqual(again["inject_mode"], "paste")

    def test_save_then_load_keeps_untouched_defaults(self):
        cfg = config.load(self.path)
        cfg["hotkey"] = "ctrl+alt+a"
        config.save(cfg, self.path)

        again = config.load(self.path)
        self.assertEqual(again["engine"], config.DEFAULTS["engine"])
        self.assertEqual(again["beam_size"], config.DEFAULTS["beam_size"])
        self.assertEqual(again["fixups"], config.DEFAULTS["fixups"])

    def test_round_trip_survives_accents_and_astral_chars(self):
        cfg = config.load(self.path)
        cfg["initial_prompt"] = "Transcricao de acao, coracao, ficcao — e um 🚀 no fim"
        config.save(cfg, self.path)
        self.assertEqual(config.load(self.path)["initial_prompt"], cfg["initial_prompt"])

    def test_file_is_written_as_readable_utf8(self):
        cfg = config.load(self.path)
        cfg["initial_prompt"] = "acentuacao é assim"
        config.save(cfg, self.path)
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("é", raw, "save() escapou os acentos; queremos ensure_ascii=False")

    def test_round_trip_of_nested_structures(self):
        cfg = config.load(self.path)
        cfg["fixups"] = [[" foo ", " bar "], ["baz", "qux"]]
        config.save(cfg, self.path)
        self.assertEqual(config.load(self.path)["fixups"], [[" foo ", " bar "], ["baz", "qux"]])

    def test_save_returns_the_path(self):
        cfg = config.load(self.path)
        self.assertEqual(Path(config.save(cfg, self.path)), self.path)

    def test_save_creates_missing_parent_dirs(self):
        deep = self.tmp / "a" / "b" / "config.json"
        config.save(config.load(deep), deep)
        self.assertTrue(deep.exists())

    def test_result_is_a_config_not_a_plain_dict(self):
        self.assertIsInstance(config.load(self.path), config.Config)


class CorruptFileTest(unittest.TestCase):
    """Arquivo quebrado nunca pode derrubar o app: sob pythonw nao ha console."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wisper_cfg_"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_falls_back(self):
        cfg = config.load(self.path)
        self.assertEqual(dict(cfg), config.DEFAULTS)

    def test_truncated_json(self):
        self.path.write_text('{"hotkey": "ctrl+alt+a"', encoding="utf-8")
        self._assert_falls_back()

    def test_not_json_at_all(self):
        self.path.write_text("isso aqui nao e json de jeito nenhum", encoding="utf-8")
        self._assert_falls_back()

    def test_empty_file(self):
        self.path.write_text("", encoding="utf-8")
        self._assert_falls_back()

    def test_json_that_is_not_an_object(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self._assert_falls_back()

    def test_json_null(self):
        self.path.write_text("null", encoding="utf-8")
        self._assert_falls_back()

    def test_invalid_utf8_bytes(self):
        # UnicodeDecodeError herda de ValueError, entao o except do load pega.
        self.path.write_bytes(b'{"hotkey": "\xff\xfe\x00nao e utf8"}')
        self._assert_falls_back()

    def test_path_is_a_directory(self):
        # exists() diz True e a leitura estoura com OSError; nao pode propagar.
        d = self.tmp / "config.json"
        d.mkdir()
        self._assert_falls_back()

    def test_partially_valid_file_still_applies_the_good_keys(self):
        self.path.write_text('{"hotkey": "ctrl+alt+a"}', encoding="utf-8")
        cfg = config.load(self.path)
        self.assertEqual(cfg["hotkey"], "ctrl+alt+a")
        self.assertEqual(cfg["engine"], config.DEFAULTS["engine"])


class SaveWritesOnlyTheDiffTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wisper_cfg_"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _written(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_untouched_config_writes_an_empty_object(self):
        config.save(config.load(self.path), self.path)
        self.assertEqual(self._written(), {})

    def test_only_the_changed_key_is_written(self):
        cfg = config.load(self.path)
        cfg["hotkey"] = "ctrl+alt+a"
        config.save(cfg, self.path)
        self.assertEqual(self._written(), {"hotkey": "ctrl+alt+a"})

    def test_two_changes_two_keys(self):
        cfg = config.load(self.path)
        cfg["hotkey"] = "ctrl+alt+a"
        cfg["log_level"] = "DEBUG"
        config.save(cfg, self.path)
        self.assertEqual(set(self._written()), {"hotkey", "log_level"})

    def test_setting_a_key_back_to_the_default_removes_it_from_the_file(self):
        cfg = config.load(self.path)
        cfg["hotkey"] = "ctrl+alt+a"
        config.save(cfg, self.path)
        cfg["hotkey"] = config.DEFAULTS["hotkey"]
        config.save(cfg, self.path)
        self.assertEqual(self._written(), {})

    def test_keys_unknown_to_the_defaults_are_preserved(self):
        cfg = config.load(self.path)
        cfg["chave_experimental"] = 42
        config.save(cfg, self.path)
        self.assertEqual(self._written(), {"chave_experimental": 42})
        self.assertEqual(config.load(self.path)["chave_experimental"], 42)

    def test_a_mutated_nested_default_counts_as_a_diff(self):
        cfg = config.load(self.path)
        cfg["fixups"] = cfg["fixups"] + [[" pull requesta ", " pull request "]]
        config.save(cfg, self.path)
        self.assertIn("fixups", self._written())

    def test_save_is_idempotent(self):
        cfg = config.load(self.path)
        cfg["engine"] = "groq"
        config.save(cfg, self.path)
        first = self.path.read_text(encoding="utf-8")
        config.save(config.load(self.path), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)


class EnsureDirsTest(unittest.TestCase):
    """ensure_dirs e redirecionado para um tmp: nao queremos mexer no projeto."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wisper_cfg_"))
        self._saved = (config.MODELS_DIR, config.LOG_DIR, config.ASSETS_DIR)
        config.MODELS_DIR = self.tmp / "models"
        config.LOG_DIR = self.tmp / "logs"
        config.ASSETS_DIR = self.tmp / "assets"

    def tearDown(self):
        config.MODELS_DIR, config.LOG_DIR, config.ASSETS_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_all_three(self):
        config.ensure_dirs()
        for d in (config.MODELS_DIR, config.LOG_DIR, config.ASSETS_DIR):
            self.assertTrue(d.is_dir(), "%s nao foi criado" % d)

    def test_is_idempotent(self):
        config.ensure_dirs()
        config.ensure_dirs()  # nao pode estourar com FileExistsError
        self.assertTrue(config.LOG_DIR.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
