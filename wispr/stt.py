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
TEMPERATURE_LADDER = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]

# Teto de espera do close() pela transcricao em voo. A bandeja chama close() na
# thread principal: travar ali e o app que nao fecha.
CLOSE_TIMEOUT_SEC = 5.0

# ~224 tokens e o corte do get_prompt; 4 chars/token e a media medida em pt-BR.
PROMPT_MAX_CHARS = 900

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

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

# Os cookies de os.add_dll_directory ficam vivos aqui de proposito. Ao contrario
# do que parece, o CPython 3.11 NAO remove o diretorio quando o cookie e coletado
# (`os._AddedDllDirectory` nao tem __del__, so `close()`), mas guardar a
# referencia e o unico jeito de a remocao nunca acontecer por acidente e de
# conseguir diagnosticar o caminho depois.
_DLL_COOKIES: list = []
CUDA_DLL_DIRS: list[str] = []
_DLL_LOCK = threading.Lock()   # preload() roda numa thread de boot: sem lock isso duplica


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
        temperature=list(TEMPERATURE_LADDER),
        # prompt SEM ACENTOS e curto: 13,4% -> 8,5% de WER; acima de 224 tokens
        # o get_prompt trunca e fica pior que prompt nenhum.
        initial_prompt=(cfg.get("initial_prompt") or None),
        # NUNCA passar hotwords: pior sozinho E cancela o ganho do initial_prompt.
    )


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
        _check_prompt(self._decode.get("initial_prompt"))

        if str(self.cfg.get("engine") or "local").lower() == "groq":
            self.backend = "groq"
            self.model_id = str(self.cfg.get("groq_model") or "whisper-large-v3-turbo")
            self.ready = True
            log.info("stt backend=groq model=%s", self.model_id)
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
            log.exception("model load failed on device=%s compute_type=%s; falling back to cpu",
                          device, ctype)
            if device == "cpu":
                raise TranscriptionError(
                    "Nao foi possivel carregar o modelo de transcricao. Veja o log.")
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
        log.info("stt backend=%s model=%s load=%.2fs warm=%.3fs",
                 self.backend, self.model_id, self.load_s, self.warm_s)

    # ---------------------------------------------------------------- interno
    def _load(self, WhisperModel, BatchedPipeline, device: str, ctype: str):
        """Carrega, monta o pipeline batched e aquece. Qualquer falha devolve a VRAM."""
        model = None
        batched = None
        try:
            model = WhisperModel(self.model_id, device=device, compute_type=ctype)
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
        text = " ".join(str(seg.text).strip() for seg in items).strip()
        seg_s = float(getattr(items[0], "end", 0.0) or 0.0) - float(getattr(items[0], "start", 0.0) or 0.0)
        single_short = len(items) == 1 and seg_s <= HALLUCINATION_MAX_SEC
        out = postprocess(text, self.cfg.get("fixups"), single_short_segment=single_short)
        log.info("stt ok backend=%s dur=%.1fs t=%.2fs rtf=%.3f chars=%d",
                 self.backend, dur, took, (took / dur if dur else 0.0), len(out))
        return out

    def _transcribe_groq(self, audio: np.ndarray) -> str:
        cfg = self.cfg
        key = str(cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
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
