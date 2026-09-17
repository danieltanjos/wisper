# -*- coding: utf-8 -*-
"""Configuração e caminhos do wisper.

Tudo mora dentro da pasta do projeto, em D:. Isso é deliberado: o Python da
Microsoft Store redireciona silenciosamente escritas em %LOCALAPPDATA% e
%APPDATA% para o sandbox do pacote, e a gente não quer nem chegar perto disso.
Ver docs/ARCHITECTURE.md secao 4.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"          # vira HF_HOME
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"
ASSETS_DIR = ROOT / "assets"

# HF_HOME precisa estar setado antes de qualquer import de huggingface_hub.
os.environ.setdefault("HF_HOME", str(MODELS_DIR))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

MUTEX_NAME = "Local\\WisprCloneDaniel"

# Motivo do ultimo config.json ignorado, ou None. O App.run() reloga isso como
# WARNING depois que o logging existe: o primeiro load() roda antes dele, e sob
# pythonw o aviso emitido aqui nao tem para onde ir.
load_error: str | None = None

# Glossario SEM ACENTOS de proposito: a versao sem acentos mediu WER 8,5% contra
# 9,8% da acentuada. Manter abaixo de 224 tokens ou o faster-whisper trunca e o
# prompt fica pior que nenhum. Ver docs/ARCHITECTURE.md secao 2.
DEFAULT_PROMPT = (
    "Transcricao de ditado de um desenvolvedor brasileiro. Termos tecnicos em ingles: "
    "endpoint, middleware, deploy, pull request, branch, commit, merge, Docker, Kubernetes, "
    "React, TypeScript, Python, API, backend, frontend, database, migration, Prisma, Vercel, "
    "CloudWatch, token, refresh token, autenticacao, webhook, payload, query, schema."
)

# Correcoes que o initial_prompt comprovadamente nao resolve: 'branch' voltou
# como 'brand' em todo modelo e toda variante de prompt testada.
DEFAULT_FIXUPS = [
    [" brand ", " branch "],
    [" Brand ", " branch "],
    ["docker campus up", "docker compose up"],
    ["Docker Campus App", "docker compose up"],
    ["Docker Campus up", "docker compose up"],
    ["mid-leware", "middleware"],
    ["midware", "middleware"],
]

DEFAULTS = {
    # --- hotkeys ---
    "hotkey": "win+a",              # "win+a" | "ctrl+alt+a"
    "stop_key": "enter",
    "cancel_key": "esc",
    # Tem que caber no ring com folga: o `take()` ainda reserva uma faixa de guarda
    # de um bloco, e o que passar do ring some sem erro nenhum. 175 < 180.
    "max_record_sec": 175,

    # --- motor de transcricao ---
    "engine": "local",              # "local" | "groq"
    "model_id": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "compute_type": "int8_float16",  # 1024 MB de VRAM, WER igual ao float16
    "device": "cuda",               # cai para "cpu" sozinho se a CUDA falhar
    "cpu_compute_type": "int8",
    "language": "pt",
    "beam_size": 1,
    "initial_prompt": DEFAULT_PROMPT,
    "fixups": DEFAULT_FIXUPS,
    "vad_filter": True,
    # Escada de temperatura do Whisper (0,0 -> 1,0) DESLIGADA. Num ditado curto o
    # avg_logprob do decode greedy cai abaixo do log_prob_threshold e o
    # faster-whisper passa a SORTEAR ate' temperatura 1,0; foi de la que sairam as
    # corridas de pontos ("Oi," seguido de 51 pontos). Ligar devolve o
    # comportamento original do faster-whisper, e com ele o mesmo audio pode dar
    # textos diferentes a cada tentativa. Ver docs/ARCHITECTURE.md secao 2.
    "temperature_fallback": False,
    "preload_model": True,          # carrega o modelo no boot em vez do 1o Win+A
    "groq_model": "whisper-large-v3-turbo",
    "groq_api_key": "",             # vazio => le da env GROQ_API_KEY
    "groq_timeout_sec": 30,

    # --- audio ---
    # O stream so' fica aberto do Win+A ao Enter/Esc. Fora disso o Windows nao
    # mostra "microfone em uso" e um headset Bluetooth volta para A2DP (musica com
    # qualidade) em vez de ficar preso no perfil maos-livres. Custo: o pre-roll
    # (rebobinar 0,35 s antes do Win+A) deixa de existir, porque nao havia captura.
    "mic_on_demand": True,
    "capture_sr": 48000,
    "target_sr": 16000,
    # O ring e' o teto REAL de uma fala: o que nao cabe nele e' sobrescrito antes
    # do Enter e some sem erro. 180 s a 48 kHz float32 sao ~34,5 MB residentes, o
    # preco certo por nunca truncar um ditado — a fala normal medida e' de 6 a 15 s,
    # mas ditar um paragrafo inteiro passa facil de 8. Ver ARCHITECTURE.md secao 2.
    "ring_sec": 180.0,
    "preroll_sec": 0.35,            # rebobina para nao cortar a primeira silaba
    "block": 480,                   # 10 ms
    "silence_rms": 3e-5,            # gate obrigatorio: headset pode dar silencio digital
    "highpass_hz": 80,
    "normalize_dbfs": -3.0,
    # Ronda do supervisor de stream e teto do backoff de reabertura. Ficavam so em
    # wispr/audio.py, onde ninguem que edita o config.json ia descobrir que existem.
    "supervise_sec": 0.5,           # abertura exclusiva alheia derruba o stream calado
    "reopen_backoff_max": 5.0,      # endpoint sumido de vez nao pode virar spin

    # --- injecao de texto ---
    "inject_mode": "auto",          # "auto" | "type" | "paste"
    "inject_threshold": 120,        # acima disso, cola em vez de digitar
    "restore_clipboard": True,
    "clipboard_restore_delay": 0.25,
    "trailing_space": False,

    # --- interface ---
    "overlay": True,
    "overlay_idle_pill": True,      # traco cinza no rodape entre ditados, como a Flow Bar
    "overlay_offset_y": 110,
    "sounds": True,
    "notify_on_error": True,

    # --- diagnostico ---
    "log_level": "INFO",
    "keep_recordings": False,       # grava o wav de cada ditado em logs/recordings
    "history_size": 50,
}


class Config(dict):
    """dict com acesso por atributo, para cfg.hotkey em vez de cfg['hotkey']."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def load(path: Path | None = None) -> Config:
    """Le config.json por cima dos defaults. Arquivo ausente ou corrompido nao
    derruba o app: ele volta aos defaults."""
    global load_error
    path = Path(path) if path else CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    load_error = None
    if path.exists():
        try:
            # utf-8-sig: Bloco de Notas e Out-File gravam UTF-8 COM BOM, e o
            # json.loads estoura com "Expecting value: line 1 column 1" no BOM.
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(user, dict):
                raise ValueError("top-level value is %s, expected an object"
                                 % type(user).__name__)
            data.update(user)
        except (OSError, ValueError) as exc:
            load_error = "%s: %s" % (type(exc).__name__, exc)
            log.warning("%s ignored, using defaults: %s", path, load_error)
    return Config(data)


def save(cfg: dict, path: Path | None = None) -> Path:
    """Grava apenas o que difere dos defaults, para o arquivo ficar legivel."""
    path = Path(path) if path else CONFIG_PATH
    diff = {k: v for k, v in cfg.items() if k not in DEFAULTS or DEFAULTS[k] != v}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(diff, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def ensure_dirs() -> None:
    for d in (MODELS_DIR, LOG_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
