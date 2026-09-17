# -*- coding: utf-8 -*-
"""Transcricao de fala do wisper.

Dois backends: faster-whisper local na RTX 4060 Ti (padrao) e a API da Groq
(`engine="groq"`), que nunca carrega modelo local. Os parametros de decodificacao
abaixo sao o resultado de um sweep medido nesta maquina, nao chute — ver
docs/ARCHITECTURE.md secao 2. Nao "melhorar" sem medir de novo.

Importar este modulo e barato de proposito: `import faster_whisper` custa
segundos, entao ele acontece dentro de Engine.__init__ e nao no topo do arquivo,
para a bandeja subir antes de o modelo estar pronto.
"""
from __future__ import annotations

import gc
import glob
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

from wispr import config

try:
    from wispr import logging_setup

    log = logging_setup.get(__name__)
except Exception:  # este modulo tem que ser importavel sozinho (testes, sweeps)
    log = logging.getLogger(__name__)


SR = 16000                    # taxa que o faster-whisper exige na entrada
WARMUP_SEC = 2.0
BATCH_MIN_SEC = 30.0          # abaixo disso o batched nao compensa a VRAM extra
BATCH_SIZE = 8
# A escada de temperatura do Whisper, DESLIGADA por padrao nesta aplicacao.
# Ver _decode_kwargs() e docs/ARCHITECTURE.md secao 2: ela e' a causa medida das
# corridas de pontuacao em ditado curto. `temperature_fallback: true` no
# config.json devolve o comportamento original do faster-whisper.
TEMPERATURE_LADDER = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
GREEDY_ONLY = [0.0]

# Teto de espera do close() pela transcricao em voo. A bandeja chama close() na
# thread principal: travar ali e o app que nao fecha.
CLOSE_TIMEOUT_SEC = 5.0

# ~224 tokens e o corte do get_prompt; 4 chars/token e a media medida em pt-BR.
PROMPT_MAX_CHARS = 900

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# O minimo que um snapshot CTranslate2 precisa ter para valer a pena carregar
# offline. Ver resolve_model_source().
#
# `tokenizer.json` esta aqui porque a falta dele nao e' um erro no
# faster-whisper: o WhisperModel cai em
# `tokenizers.Tokenizer.from_pretrained("openai/whisper-tiny")`
# (transcribe.py:700-707), que BAIXA da rede -- exatamente o round trip que
# resolve_model_source() existe para eliminar -- e ainda entrega o vocabulario
# errado, porque o large-v3 tem um token de idioma a mais que o tiny. Sem
# tokenizer.json o caminho online e' melhor: o download_model repara o snapshot.
# `preprocessor_config.json` NAO entra: o Systran/faster-whisper-small em cache
# nesta maquina nao tem esse arquivo e esta' correto, porque 80 mel bins e' o
# default do FeatureExtractor. Exigi-lo mandaria esse modelo para a rede todo
# boot.
SNAPSHOT_FILES = ("model.bin", "config.json", "tokenizer.json")

# Alucinacao classica do Whisper em audio vazio/quase vazio. So e aplicada a um
# unico segmento curto — fala real nunca e censurada.
HALLUCINATION_MAX_SEC = 4.0
HALLUCINATION_MAX_CHARS = 64
_HALLUCINATIONS = frozenset({
    "legendas pela comunidade amara.org",
    "amara.org",
    "obrigado",
    "obrigada",
    "tchau",
})

_WS = re.compile(r"\s+")

# ---------------------------------------------------------- corridas de pontuacao
# O large-v3-turbo tem um decoder destilado de 4 camadas e entra em loop de "."
# quando a janela de 30 s tem pouca fala -- exatamente o formato de um ditado
# real (Win+A, pausa, duas palavras, pausa, Enter). A rede de seguranca real e'
# o decode greedy (ver _decode_kwargs); isto aqui e' o cinto, porque o modelo
# sempre vai poder surpreender.
#
# A REGRA, de proposito conservadora, em duas partes:
#   1. Corrida de 3+ caracteres de pontuacao IGUAIS colapsa. O ponto colapsa
#      para exatamente TRES ("..." e a unica sequencia de pontuacao repetida que
#      existe na ortografia do pt-BR, entao uma reticencia ditada sobrevive
#      byte a byte); qualquer outro caractere colapsa para um so, porque ",,,",
#      ";;;" e ":::" nao sao ortografia e "!!!"/"???" nao saem de FALA.
#   2. So depois disso, uma cauda de pontos e' removida em dois casos que nunca
#      sao texto legitimo: quando ela esta SOLTA (separada da ultima palavra por
#      espaco, com 3+ pontos no total -- "cd .." tem dois e fica intacto) e
#      quando ela esta EMPILHADA em cima de outra pontuacao ("Oi,..." -> "Oi,").
# Tudo que esta colado numa palavra fica: "Espera ai...", "Que?!", "1.000,00",
# "arquivo.tar.gz" e "https://ex.com/docs/..." passam sem um caractere alterado.
_PUNCT_RUN_CHARS = ".,;:!?\u2026"
_PUNCT_RUN_RE = re.compile(r"([%s])\1{2,}" % re.escape(_PUNCT_RUN_CHARS))
_DOT_TAIL_STACKED_RE = re.compile(r"(?<=[,;:!?])\s*[.\u2026]+\s*$")
_DOTS = ".\u2026"
MIN_DETACHED_DOTS = 3


def _collapse_punct_run(match) -> str:
    return "..." if match.group(1) == "." else match.group(1)


def _detached_tail_start(text: str) -> int:
    """Onde comeca a cauda de pontos SOLTA, ou -1 se nao houver nenhuma.

    Equivale a `(?:\\s+[.\u2026]+)+\\s*$`, e e' feito na mao justamente por isso:
    aquele padrao tem quantificador aninhado e o `search` o experimenta em TODA
    posicao do texto. Quando o modelo entra em loop de ". . . ." e a fala NAO
    termina no loop nao existe casamento nenhum, entao ele varre o texto inteiro
    a cada posicao -- O(n^2). Medido nesta maquina: 20 ms para 2 kB, 305 ms para
    8 kB e 1,22 s para 16 kB, num texto que o `max_record_sec` de 175 s permite.
    A varredura de tras para frente da' o mesmo resultado (equivalencia
    verificada por fuzz diferencial em 400 mil strings, zero divergencias) em
    0,006 ms.
    """
    i = len(text)
    while i and (text[i - 1] in _DOTS or text[i - 1].isspace()):
        i -= 1
    # A cauda so' e' SOLTA a partir do primeiro espaco dela: ponto colado na
    # palavra ("Espera ai...") nunca e' cauda solta, e e' esse o pedaco que o
    # `\s+` do padrao original exigia antes dos pontos.
    j = i
    while j < len(text) and not text[j].isspace():
        j += 1
    if j >= len(text):
        return -1
    tail = text[j:]
    if sum(tail.count(ch) for ch in _DOTS) < MIN_DETACHED_DOTS:
        return -1
    return j


def strip_punct_runs(text: str) -> str:
    """Desarma corrida de pontuacao sem nunca encostar em texto legitimo."""
    out = _PUNCT_RUN_RE.sub(_collapse_punct_run, text)
    start = _detached_tail_start(out)
    if start >= 0:
        out = out[:start]
    # A cauda EMPILHADA vem DEPOIS da solta, de proposito: o modelo devolve varios
    # grupos de pontos ("Alo,.... ...."), a solta corta so a partir do espaco e
    # deixa "Alo,..." -- que e' exatamente a empilhada. Na ordem inversa vazava.
    out = _DOT_TAIL_STACKED_RE.sub("", out)
    return out.strip()


# Os cookies de os.add_dll_directory ficam vivos aqui de proposito. Ao contrario
# do que parece, o CPython 3.11 NAO remove o diretorio quando o cookie e coletado
# (`os._AddedDllDirectory` nao tem __del__, so `close()`), mas guardar a
# referencia e o unico jeito de a remocao nunca acontecer por acidente e de
# conseguir diagnosticar o caminho depois.
_DLL_COOKIES: list = []
CUDA_DLL_DIRS: list[str] = []
_DLL_LOCK = threading.Lock()   # preload() roda numa thread de boot: sem lock isso duplica


def _prepend_path(dirs: list[str]) -> None:
    """Poe `dirs` na frente do %PATH% do processo, sem duplicar.

    So mexe em os.environ, que e' local a este processo: nada e' gravado no
    registro e nenhum outro programa e' afetado.
    """
    current = os.environ.get("PATH", "")
    have = {p.casefold() for p in current.split(os.pathsep) if p}
    missing = [d for d in dirs if d.casefold() not in have]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + ([current] if current else []))


class TranscriptionError(RuntimeError):
    """Falha de transcricao com mensagem em pt-BR que a bandeja pode exibir."""


# --------------------------------------------------------------------- CUDA DLLs
def _add_cuda_dll_dirs() -> list[str]:
    """Poe os `bin` das wheels nvidia/* no caminho de busca de DLL do Windows.

    `ctranslate2.dll` carrega `cublas64_12.dll` DINAMICAMENTE (nao esta na import
    table do PE), entao o modelo carrega normalmente em `cuda` e so o
    `transcribe()` estoura com "Library cublas64_12.dll is not found". Tem que
    rodar ANTES de `from faster_whisper import ...`.
    `nvidia` e namespace package -> `__file__` e None -> usar `__path__`.
    """
    if sys.platform != "win32":
        return CUDA_DLL_DIRS
    with _DLL_LOCK:
        if CUDA_DLL_DIRS:
            return CUDA_DLL_DIRS
        try:
            import nvidia

            roots = list(nvidia.__path__)
        except Exception:
            log.warning("nvidia wheels not importable; cuda dll dirs not added")
            return CUDA_DLL_DIRS
        for root in roots:
            try:
                candidates = sorted(glob.glob(os.path.join(root, "*", "bin")))
            except OSError as exc:
                log.warning("could not scan %s for cuda dlls: %s", root, exc)
                continue
            for path in candidates:
                if not os.path.isdir(path):
                    continue
                try:
                    _DLL_COOKIES.append(os.add_dll_directory(path))
                    CUDA_DLL_DIRS.append(path)
                except OSError as exc:
                    log.warning("add_dll_directory failed for %s: %s", path, exc)
        # O add_dll_directory sozinho NAO resolve, e isso foi medido nesta maquina:
        # ele so vale para quem chama LoadLibraryEx com LOAD_LIBRARY_SEARCH_USER_DIRS.
        # O ctypes faz isso (ctypes.WinDLL('cublas64_12.dll') carregava normalmente),
        # mas o ctranslate2.dll usa LoadLibrary simples, que ignora esses diretorios
        # e cai na ordem de busca classica -- onde o PATH entra. Sem esta linha o
        # modelo carrega em cuda e so o transcribe() estoura com "Library
        # cublas64_12.dll is not found or cannot be loaded", e o Engine cai para CPU
        # (6x mais lento: RTF 0,35 contra 0,055 na mesma fala de 15 s).
        if CUDA_DLL_DIRS:
            _prepend_path(CUDA_DLL_DIRS)
        log.debug("cuda dll dirs: %d", len(CUDA_DLL_DIRS))
        return CUDA_DLL_DIRS


# Roda no import: assim qualquer ordem de import do app ja chega com as DLLs no
# lugar. Nunca pode levantar — sob pythonw.exe um erro aqui derruba o app inteiro
# no import, sem console para mostrar o traceback.
try:
    _add_cuda_dll_dirs()
except Exception:  # pragma: no cover
    log.exception("_add_cuda_dll_dirs failed at import time")


# ---------------------------------------------------------------- pos-processamento
def _is_hallucination(text: str) -> bool:
    key = text.strip().lower()
    if not key:
        return True
    if not key.strip(" .…!?"):        # so reticencias: "...", "…"
        return True
    return key.rstrip(" .…!?") in _HALLUCINATIONS


def postprocess(text: str, fixups, *, single_short_segment: bool = False) -> str:
    """Aplica os fixups do config, colapsa espacos e tira as bordas.

    `single_short_segment=True` (so o Engine passa isso, por keyword, quando a
    transcricao veio de um unico segmento curto) libera tambem o descarte da
    blacklist de alucinacao. Por padrao fica desligado para nunca censurar fala
    real; a chamada de duas posicoes do CONTRACT.md nunca censura nada.
    """
    if not text:
        return ""
    pairs = fixups.items() if isinstance(fixups, dict) else (fixups or ())
    # Os fixups casam com o espaco em volta (" brand " -> " branch "), dai o padding.
    out = " " + str(text) + " "
    for pair in pairs:
        try:
            src, dst = pair
        except (TypeError, ValueError):
            log.warning("ignoring malformed fixup: %r", pair)
            continue
        out = out.replace(str(src), str(dst))
    out = _WS.sub(" ", out).strip()
    # Depois dos fixups e do colapso de espaco: a regra de cauda precisa enxergar
    # " ..." com UM espaco so, e um fixup pode ser justamente quem cria a cauda.
    before = out
    out = strip_punct_runs(out)
    if out != before:
        log.info("punctuation run collapsed: %r -> %r", before[:120], out[:120])
    if out and not any(ch.isalnum() for ch in out):
        # So pontuacao ("...", ". . .", "?!") nunca e' fala: e' o loop do decoder
        # num clipe quase vazio. Nao e' censura -- nao ha uma letra para censurar.
        log.info("dropped punctuation-only transcript: %r", out[:60])
        return ""
    if single_short_segment and len(out) <= HALLUCINATION_MAX_CHARS and _is_hallucination(out):
        log.info("dropped hallucination on near-empty audio: %r", out)
        return ""
    return out


def _decode_kwargs(cfg) -> dict:
    """Parametros de decodificacao medidos. Ver docs/ARCHITECTURE.md secao 2."""
    return dict(
        language=str(cfg.get("language") or "pt"),   # auto-detect custa ~0,2 s por clipe a troco de nada
        beam_size=int(cfg.get("beam_size") or 1),    # beam 5 ganha <=1,2pp de WER e custa ~0,3 s
        condition_on_previous_text=False,
        vad_filter=bool(cfg.get("vad_filter", True)),
        # Greedy puro. A escada de temperatura e' a causa MEDIDA das corridas de
        # pontuacao do ditado curto, e nao um detalhe de gosto: num ditado real
        # (poucas palavras dentro da janela de 30 s) o avg_logprob do decode
        # greedy fica abaixo do log_prob_threshold (-1,0) por padrao, entao o
        # faster-whisper marca needs_fallback e SORTEIA ate' temperatura 1,0.
        # Nenhum dos outros guarda-chuvas pega o estrago: a compressao gzip de
        # "Faca deploy .........." da 1,0 contra um limiar de 2,4 (a corrida e'
        # curta demais para o gzip enxergar) e o no_speech_prob volta 0,0 porque
        # ha fala de verdade no clipe. Aos 6 niveis ele ainda escolhe por
        # avg_logprob entre amostras de temperaturas diferentes, que nao sao
        # comparaveis entre si -- foi assim que "Oi," virou "Oi," e 51 pontos.
        # Com [0.0] o laco de fallback devolve sempre o unico decode greedy: o
        # mesmo audio passa a dar sempre o mesmo texto, o que uma ferramenta de
        # ditado precisa.
        #
        # MEDIDO nesta maquina, com o modelo carregado, em dois clipes sinteticos
        # de 4,9 s montados a partir de logs/recordings (fala curta + ruido de
        # sala real), 12 tentativas cada:
        #   escada: 4/12 e 6/12 com corrida de pontos, 4 e 7 textos DIFERENTES
        #           para o mesmo wav (a amostragem do ctranslate2 nao tem
        #           semente fixada aqui);
        #   greedy: 0/12 e 0/12, um unico texto.
        # Numa varredura de 972 clipes de 2-6 s a escada ainda produziu 9 loops
        # de palavra ('tchau, tchau, tchau') contra 1 do greedy -- ou seja, ela
        # piora a repeticao em vez de resgatar dela.
        # WER nas tres fixtures: IDENTICO, texto por texto (0,0 / 10,7 / 0,0;
        # media 3,6%). As tres ja decodificavam em temperatura 0,0.
        temperature=(list(TEMPERATURE_LADDER) if cfg.get("temperature_fallback")
                     else list(GREEDY_ONLY)),
        # prompt SEM ACENTOS e curto: 13,4% -> 8,5% de WER; acima de 224 tokens
        # o get_prompt trunca e fica pior que prompt nenhum.
        initial_prompt=(cfg.get("initial_prompt") or None),
        # NUNCA passar hotwords: pior sozinho E cancela o ganho do initial_prompt.
    )


def resolve_model_source(model_id: str) -> tuple[str, bool]:
    """Devolve (o que entregar ao WhisperModel, veio do cache local?).

    Passar o `model_id` cru faz o faster-whisper bater em
    `huggingface.co/api/models/<id>/revision/main` em TODO boot so para confirmar
    a revisao -- um round trip de rede por inicializacao, num app que tem que
    subir sem conexao com o modelo inteiro em models/. Com `local_files_only=True`
    o download_model resolve o snapshot em disco (0,14 s medidos, sem rede) e
    devolve o diretorio; um diretorio faz o WhisperModel pular o Hub inteiro.

    Modelo ainda nao baixado levanta LocalEntryNotFoundError -- ai a unica saida
    correta e' o caminho online, com o model_id cru. Nunca levanta: sob
    pythonw.exe quem chama nao tem console para ver o traceback.
    """
    mid = str(model_id or "")
    if not mid:
        return mid, False
    if os.path.isdir(mid):
        return mid, True            # caminho local explicito no config.json
    try:
        from faster_whisper.utils import download_model

        path = str(download_model(mid, local_files_only=True))
    except Exception as exc:
        log.info("model %s is not in the local cache at %s (%s); loading online",
                 mid, os.environ.get("HF_HOME", "?"), exc.__class__.__name__)
        return mid, False
    # Cache pela metade (download interrompido) e' o unico jeito de esta troca
    # piorar as coisas: o caminho online repararia sozinho, o offline entregaria
    # um diretorio sem peso e o Engine cairia para CPU achando que foi VRAM.
    missing = [f for f in SNAPSHOT_FILES if not os.path.isfile(os.path.join(path, f))]
    if missing:
        log.warning("snapshot %s is incomplete (missing %s); loading online",
                    path, ", ".join(missing))
        return mid, False
    log.info("model resolved offline: %s", path)
    return path, True


def _check_prompt(prompt) -> None:
    """Avisa sobre os dois achados do sweep que o codigo sozinho nao mostra."""
    text = str(prompt or "")
    if not text:
        return
    if any(ord(ch) > 127 for ch in text):
        log.warning("initial_prompt has accents: measured WER 9.8 against 8.5 percent "
                    "for the same prompt without them")
    if len(text) > PROMPT_MAX_CHARS:
        log.warning("initial_prompt is %d chars: get_prompt truncates at 224 tokens and a "
                    "truncated prompt measured worse than no prompt at all", len(text))


def _as_mono_float32(audio) -> np.ndarray:
    """Normaliza a entrada para float32 mono em [-1,1] sem nunca levantar por tipo.

    Inteiro cru (int16 de um WAV, por exemplo) tem que ser escalado: o log-mel do
    Whisper NAO e invariante a ganho (desloca 0,5 por decada), entao amplitude
    32768x nao da erro nenhum — so devolve lixo.
    """
    arr = np.asarray(audio)
    if arr.dtype.kind in "iu":
        info = np.iinfo(arr.dtype)
        arr = arr.astype(np.float32) / float(max(abs(int(info.min)), int(info.max)))
        if info.min == 0:                       # unsigned: 0..max centrado em meia escala
            arr = arr * 2.0 - 1.0
    if arr.ndim > 1:
        arr = arr.mean(axis=-1)                 # mixdown; ravel() entrelacaria os canais
    x = np.ascontiguousarray(np.asarray(arr, dtype=np.float32).ravel())
    if x.size:
        bad = ~np.isfinite(x)
        if bad.any():
            # NaN/Inf de um driver quebrado envenena o log-mel inteiro em silencio.
            log.warning("audio has %d non-finite samples; zeroing them", int(bad.sum()))
            x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
    return x


# ------------------------------------------------------------------------- Groq
def _wav_bytes(audio: np.ndarray, sr: int = SR) -> bytes:
    """Empacota float32 [-1,1] como WAV PCM 16-bit mono na memoria."""
    pcm = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    buf = io.BytesIO()
    # wave.open com file object nao fecha o BytesIO no close(): getvalue() abaixo e valido.
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sr))
        wav.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


def _multipart(fields: dict, file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """Monta multipart/form-data na mao — nao vale adicionar `requests` por isso."""
    boundary = "----wispr" + uuid.uuid4().hex
    mark = b"--" + boundary.encode("ascii")
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        if value is None or value == "":
            continue
        body += mark + crlf
        body += ('Content-Disposition: form-data; name="%s"' % name).encode("ascii") + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    body += mark + crlf
    body += ('Content-Disposition: form-data; name="%s"; filename="%s"'
             % (file_field, filename)).encode("ascii") + crlf
    body += b"Content-Type: audio/wav" + crlf + crlf
    body += payload + crlf
    body += mark + b"--" + crlf
    return bytes(body), boundary


# ----------------------------------------------------------------------- Engine
def groq_key(cfg) -> str:
    """Chave do Groq: config.json, senao GROQ_API_KEY (o config.py ja leu o .env)."""
    return str(cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()


class Engine:
    """Motor de transcricao residente. Um modelo, varias threads possiveis."""

    def __init__(self, cfg=None):
        self.cfg = cfg or config.load()
        self._lock = threading.Lock()
        self._decode = _decode_kwargs(self.cfg)
        self._closed = False
        self.ready = False
        self.backend = ""
        self.model_id = ""
        self.load_s = 0.0
        self.warm_s = 0.0
        self.model = None
        self.batched = None
        self.model_source = ""     # snapshot em disco, quando ele ja existe
        self.offline = False       # True = o boot nao falou com a rede
        _check_prompt(self._decode.get("initial_prompt"))

        engine = str(self.cfg.get("engine") or "auto").lower()
        if engine == "groq":
            self._use_groq()
            return

        self.model_id = str(self.cfg.get("model_id") or config.DEFAULTS["model_id"])
        if "distil" in self.model_id.lower():
            # Medido: o distil traduz pt-BR para ingles silenciosamente (WER 73-106%).
            log.warning("model_id %r is a distil build: it silently translates pt-BR to English",
                        self.model_id)

        _add_cuda_dll_dirs()
        t0 = time.perf_counter()
        from faster_whisper import BatchedInferencePipeline, WhisperModel  # import caro: fica aqui de proposito

        log.info("faster_whisper imported in %.2fs", time.perf_counter() - t0)

        # Depois do import (o download_model vem de dentro do faster_whisper) e
        # antes do _load: os dois caminhos, cuda e o fallback cpu, usam o mesmo
        # snapshot e nao podem resolver o Hub duas vezes.
        self.model_source, self.offline = resolve_model_source(self.model_id)

        want = str(self.cfg.get("device") or "cuda").strip().lower()
        cpu_ctype = str(self.cfg.get("cpu_compute_type") or "int8")
        if want.startswith("cpu"):
            # Nao tentar cuda: com device=cpu no config o compute_type de GPU
            # (int8_float16) seria invalido e so encheria o log de traceback.
            device, ctype = "cpu", cpu_ctype
        else:
            device = "cuda"           # o contrato so aceita "cuda"|"cpu"|"groq"
            ctype = str(self.cfg.get("compute_type") or "int8_float16")

        t0 = time.perf_counter()
        try:
            self.model, self.batched, self.warm_s = self._load(
                WhisperModel, BatchedInferencePipeline, device, ctype)
            self.backend = device
        except Exception:
            # So ~1,5 GB de VRAM livre com o desktop em uso: OOM aqui e caminho
            # esperado, nao bug. E a falha do cublas so aparece quando o encoder
            # roda, por isso o warmup faz parte da tentativa.
            log.exception("model load failed on device=%s compute_type=%s; falling back",
                          device, ctype)
            if device == "cpu":
                raise TranscriptionError(
                    "Nao foi possivel carregar o modelo de transcricao. Veja o log.")
            if engine == "auto" and groq_key(self.cfg):
                # Sem GPU e com chave: nuvem antes da CPU. Medido no notebook:
                # a CPU custa 6,5-8 s FIXOS por ditado (encoder da janela de 30 s)
                # e nenhum ajuste local muda isso; ver CLAUDE.md.
                log.info("engine=auto: no cuda, GROQ_API_KEY present -> groq instead of cpu")
                self._use_groq()
                return
            try:
                self.model, self.batched, self.warm_s = self._load(
                    WhisperModel, BatchedInferencePipeline, "cpu", cpu_ctype)
                self.backend = "cpu"
            except Exception as exc:
                log.exception("model load failed on cpu compute_type=%s", cpu_ctype)
                raise TranscriptionError(
                    "Nao foi possivel carregar o modelo de transcricao (%s). Veja o log."
                    % exc.__class__.__name__) from exc

        self.load_s = time.perf_counter() - t0
        self.ready = True
        log.info("stt backend=%s model=%s offline=%s load=%.2fs warm=%.3fs",
                 self.backend, self.model_id, self.offline, self.load_s, self.warm_s)

    def _use_groq(self) -> None:
        self.backend = "groq"
        self.model_id = str(self.cfg.get("groq_model") or "whisper-large-v3-turbo")
        self.ready = True
        log.info("stt backend=groq model=%s", self.model_id)

    # ---------------------------------------------------------------- interno
    def _load(self, WhisperModel, BatchedPipeline, device: str, ctype: str):
        """Carrega, monta o pipeline batched e aquece. Qualquer falha devolve a VRAM."""
        model = None
        batched = None
        try:
            model = WhisperModel(self.model_source or self.model_id,
                                 device=device, compute_type=ctype)
            try:
                batched = BatchedPipeline(model=model)
            except Exception:
                log.exception("BatchedInferencePipeline unavailable; long audio stays sequential")
                batched = None

            silence = np.zeros(int(SR * WARMUP_SEC), dtype=np.float32)
            t0 = time.perf_counter()
            # vad_filter=False DE PROPOSITO, e nao e detalhe: com o VAD ligado o
            # silencio nao gera nenhum speech chunk, o faster-whisper fica com
            # content_frames == 0 e sai de generate_segments SEM chamar o encoder.
            # Ou seja: o cublas64_12.dll nunca seria carregado aqui, a falha
            # medida da secao 2 escaparia do fallback cuda->cpu e so estouraria no
            # primeiro ditado do usuario. Sem VAD o encoder roda na janela de 30 s.
            warm = dict(self._decode)
            warm["vad_filter"] = False
            list(model.transcribe(silence, **warm)[0])
            warm_s = time.perf_counter() - t0

            if self._decode.get("vad_filter"):
                # Segunda passada so para criar a sessao ONNX do Silero no boot em
                # vez de no primeiro ditado. Nao roda o encoder, entao falhar aqui
                # nao e motivo para condenar a GPU e recarregar tudo na CPU.
                try:
                    t1 = time.perf_counter()
                    list(model.transcribe(silence, **self._decode)[0])
                    warm_s += time.perf_counter() - t1
                except Exception:
                    log.exception("vad warmup failed; the first dictation pays for the ONNX session")
            return model, batched, warm_s
        except Exception:
            _release(model)
            # O traceback segura este frame: sem zerar os dois locais o pipeline
            # continua apontando para o modelo e a VRAM fica presa.
            model = None
            batched = None
            gc.collect()
            raise

    def _transcribe_local(self, audio: np.ndarray) -> str:
        dur = len(audio) / SR
        t0 = time.perf_counter()
        with self._lock:
            model, batched = self.model, self.batched
            if model is None:
                raise TranscriptionError("O motor de transcricao ja foi encerrado.")
            items = None
            try:
                if dur > BATCH_MIN_SEC and batched is not None:
                    try:
                        # Batched so compensa passando de ~30 s de audio.
                        items = list(batched.transcribe(audio, batch_size=BATCH_SIZE,
                                                        **self._decode)[0])
                    except Exception as exc:
                        # Batched aloca mais VRAM: pode estourar onde o sequencial passa.
                        log.warning("batched transcribe failed (%s); falling back to sequential", exc)
                        items = None
                if items is None:
                    items = list(model.transcribe(audio, **self._decode)[0])
            except Exception as exc:
                log.exception("transcribe failed on backend=%s dur=%.1fs", self.backend, dur)
                raise TranscriptionError(
                    "Falha ao transcrever (%s). Veja o log." % exc.__class__.__name__) from exc

        took = time.perf_counter() - t0
        if not items:
            log.info("stt empty backend=%s dur=%.1fs t=%.2fs", self.backend, dur, took)
            return ""
        # Segmento sem uma letra ou digito sequer e' o decoder em loop de "."
        # (medido: e' um segmento A MAIS ao lado da fala real, nao o clipe inteiro,
        # por isso no_speech_prob volta 0,0). Sai ANTES do join: colado no meio
        # ("Alo, .... Isso de") nenhuma regra de cauda o alcancaria.
        texts = [str(seg.text).strip() for seg in items]
        kept = [t for t in texts if any(ch.isalnum() for ch in t)]
        if len(kept) != len(texts):
            log.info("dropped %d punctuation-only segment(s): %r",
                     len(texts) - len(kept), [t[:40] for t in texts if t not in kept])
        text = " ".join(kept).strip()
        seg_s = float(getattr(items[0], "end", 0.0) or 0.0) - float(getattr(items[0], "start", 0.0) or 0.0)
        single_short = len(items) == 1 and seg_s <= HALLUCINATION_MAX_SEC
        out = postprocess(text, self.cfg.get("fixups"), single_short_segment=single_short)
        log.info("stt ok backend=%s dur=%.1fs t=%.2fs rtf=%.3f chars=%d",
                 self.backend, dur, took, (took / dur if dur else 0.0), len(out))
        # So em DEBUG: e' o texto do usuario. Sem isto nao ha como saber que
        # forma exata uma corrida de pontos tinha quando ela vaza.
        log.debug("stt segments=%r -> %r", [t[:60] for t in texts], out[:200])
        return out

    def _transcribe_groq(self, audio: np.ndarray) -> str:
        cfg = self.cfg
        key = groq_key(cfg)
        if not key:
            raise TranscriptionError(
                "Groq sem chave: preencha groq_api_key no config.json ou defina GROQ_API_KEY.")
        fields = {
            "model": self.model_id,
            "language": str(cfg.get("language") or "pt"),
            "prompt": str(cfg.get("initial_prompt") or ""),
            "response_format": "json",
        }
        body, boundary = _multipart(fields, "file", "audio.wav", _wav_bytes(audio))
        req = urllib.request.Request(GROQ_URL, data=body, method="POST")
        # A chave vive so neste header: nenhum log ou except abaixo imprime headers.
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
        req.add_header("Accept", "application/json")

        timeout = float(cfg.get("groq_timeout_sec") or 30)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:   # antes de OSError: HTTPError herda de URLError
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            log.error("groq http %s: %s", exc.code, detail)
            raise TranscriptionError("Groq respondeu HTTP %s. Veja o log." % exc.code) from None
        except OSError as exc:  # URLError, timeout, DNS, TLS
            log.error("groq request failed: %s: %s", exc.__class__.__name__, exc)
            raise TranscriptionError(
                "Groq inacessivel (%s). Veja o log." % exc.__class__.__name__) from None
        except Exception as exc:  # HTTPException e afins nao sao OSError
            log.exception("groq request crashed")
            raise TranscriptionError(
                "Groq falhou (%s). Veja o log." % exc.__class__.__name__) from None

        try:
            text = str(json.loads(raw.decode("utf-8", "replace")).get("text") or "")
        except (ValueError, AttributeError):
            log.error("groq returned an unexpected body (%d bytes)", len(raw))
            raise TranscriptionError("Groq devolveu uma resposta invalida.") from None

        dur = len(audio) / SR
        log.info("groq ok dur=%.1fs t=%.2fs chars=%d", dur, time.perf_counter() - t0, len(text))
        single_short = dur <= HALLUCINATION_MAX_SEC and len(text.strip()) <= HALLUCINATION_MAX_CHARS
        return postprocess(text, cfg.get("fixups"), single_short_segment=single_short)

    # ---------------------------------------------------------------- publico
    def transcribe(self, audio: np.ndarray) -> str:
        """Transcreve float32 mono 16 kHz em [-1,1]. Levanta TranscriptionError."""
        if self._closed:
            raise TranscriptionError("O motor de transcricao ja foi encerrado.")
        try:
            # Coercao em vez de assert: sob -O o assert some, e aqui nao pode crashar.
            x = _as_mono_float32(audio)
        except Exception as exc:
            log.exception("could not coerce the input audio")
            raise TranscriptionError("Audio invalido para transcricao.") from exc
        if x.size == 0:
            log.info("transcribe called with empty audio")
            return ""
        if self.backend == "groq":
            return self._transcribe_groq(x)
        return self._transcribe_local(x)

    def close(self) -> None:
        """Solta o modelo e tenta devolver a VRAM na hora, sem esperar o GC.

        Nao bloqueia para sempre: a bandeja chama isto na thread principal e o
        lock pode estar com uma transcricao de varios minutos na CPU.
        """
        got = self._lock.acquire(timeout=CLOSE_TIMEOUT_SEC)
        try:
            self._closed = True
            self.ready = False
            model, self.model, self.batched = self.model, None, None
        finally:
            if got:
                self._lock.release()
        if got:
            _release(model)
        elif model is not None:
            # unload_model() com um decode em voo liberaria a memoria debaixo do
            # ctranslate2 — isso e crash de processo, nao excecao. So larga a
            # referencia: o GC devolve a VRAM quando a transcricao terminar.
            log.warning("close(): transcription still running after %.0fs; releasing by refcount",
                        CLOSE_TIMEOUT_SEC)
        model = None
        gc.collect()
        log.info("stt engine closed (backend=%s)", self.backend)


def _release(model) -> None:
    """Devolve a VRAM do modelo; o caller ainda precisa largar a referencia dele."""
    if model is None:
        return
    try:
        model.model.unload_model()   # ct2 devolve a VRAM aqui; o GC sozinho e preguicoso
    except Exception:
        pass


def preload(cfg=None) -> Engine:
    """Sobe o Engine (carrega + aquece) para o app chamar numa thread de boot."""
    cfg = cfg or config.load()
    t0 = time.perf_counter()
    try:
        engine = Engine(cfg)
    except Exception:
        log.exception("stt preload failed")
        raise
    log.info("stt preload done in %.2fs (backend=%s)", time.perf_counter() - t0, engine.backend)
    return engine
