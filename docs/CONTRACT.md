# Contrato entre os módulos

Todo módulo em `wispr/` expõe exatamente a API abaixo. Nenhum agente pode mudar a
assinatura de outro módulo; se algo estiver faltando, resolva dentro do seu próprio
arquivo. O integrador (`wispr/app.py`) só depende do que está escrito aqui.

Ordem de import é crítica e está fixada em `wispr/__init__.py`. **Nunca** importe
`sounddevice` antes de `comtypes`, e **nunca** importe `faster_whisper` antes de
`wispr.stt._add_cuda_dll_dirs()` ter rodado.

---

## `wispr/config.py` — pronto, não mexer

```python
ROOT: Path            # raiz do projeto
MODELS_DIR: Path      # HF_HOME, já exportado como env var no import
LOG_DIR: Path
ASSETS_DIR: Path
CONFIG_PATH: Path
MUTEX_NAME: str
DEFAULTS: dict
class Config(dict)    # acesso por atributo: cfg.hotkey
def load(path=None) -> Config
def save(cfg, path=None) -> Path
def ensure_dirs() -> None
```

## `wispr/logging_setup.py`

```python
def setup(level: str = "INFO") -> logging.Logger
    """RotatingFileHandler em LOG_DIR/wisper.log, 1 MB, 5 backups, utf-8.
    Formato: '%(asctime)s %(threadName)-10s %(levelname)-5s %(name)s: %(message)s'.
    Instala também sys.excepthook e threading.excepthook para que exceção em
    qualquer thread apareça no log — sob pythonw.exe não existe console."""

def get(name: str) -> logging.Logger
```

## `wispr/hotkey.py`

```python
class HotkeyEngine:
    def __init__(self, on_start, on_stop, on_cancel, cfg=None, *, on_error=None): ...
    def start(self) -> "HotkeyEngine"   # instala o hook, retorna self
    def stop(self) -> None
    recording: bool                      # o app seta ao entrar/sair de gravação
    cancellable: bool                    # enquanto True o Esc cancela; dono da
                                         # limpeza é o app, igual a `recording`
    installs: int                        # instalações BEM-SUCEDIDAS, monotônico
    failed_installs: int                 # SetWindowsHookExW que voltou 0
    alive: bool                          # property: existe um WH_KEYBOARD_LL nosso
```

Callbacks são chamados na thread worker interna do engine, nunca no hook proc.

`on_error(reason: str)` é opcional e **keyword-only**: o engine chama quando o
hook para de instalar. Ele não tem timer de volta ao normal — quem mostra o erro
(a bandeja) tem que reavaliar `alive` a cada repintura e **desfazer** o aviso
sozinho quando o watchdog reinstalar o hook.

Saúde do hook, para o app e a bandeja lerem ao vivo: `alive` (False = o atalho
não chega mais), `installs` (> 1 significa que o watchdog já reinstalou) e
`failed_installs` (cresce a cada recusa). `alive` tem um limite honesto: quando
o Windows despeja o hook em silêncio ninguém avisa, e ele continua True até a
reinstalação.

## `wispr/audio.py`

```python
CAPTURE_SR: int
TARGET_SR: int

class Mic:
    def __init__(self, cfg=None, on_event=lambda *a, **k: None): ...
    name: str        # friendly name do endpoint em uso
    device: int      # índice sounddevice
    sr: int          # taxa de captura REAL em uso (48000 aqui)
    reopens: int
    stream: object | None      # o sd.InputStream vivo; None enquanto reabre
    last_audio: np.ndarray     # último take() cru, só diagnóstico
    def mark(self) -> int                       # marca início, já com o pré-roll
    def take(self, start: int) -> tuple[np.ndarray, str]   # (áudio 48k, 'ok'|'lapped'|'empty')
    def tail(self, seconds: float = 0.1) -> np.ndarray     # últimas N s, NÃO consome
    def level(self, seconds: float = 0.1) -> float         # RMS pronto p/ Overlay.set_level
    @staticmethod
    def has_signal(x: np.ndarray, threshold: float | None = None) -> bool
                                                # gate de RMS contra silêncio digital
    def report(self) -> dict                    # diagnóstico do menu da bandeja
    def close(self) -> None

def rms(x) -> float
def to_whisper(x48: np.ndarray) -> np.ndarray   # 48k float32 -> 16k float32 pronto p/ Whisper
```

`stream`, `sr` e `last_audio` estão aqui porque o `app.py` depende dos três: o
supervisor lê `stream.active` para saber se a captura está de pé (uma abertura
WASAPI exclusiva alheia derruba o nosso stream sem erro nenhum), `sr` é a taxa
**real** e não se deve assumir `CAPTURE_SR`, e `last_audio` é o único jeito de
medir o áudio cru depois de um `take()` que devolveu 'empty'.

O medidor da pílula usa `level()`/`tail()` e **nunca** `mark()`/`take()`:
`take()` é a leitura da gravação — aplica o gate de silêncio, emite eventos e
reescreve `last_audio`. Chamado 10x por segundo ele apaga o diagnóstico do
ditado dentro do log rotativo de 1 MB.

O ring é o teto real de uma fala: `ring_sec` (180 s por padrão) sobrescreve o
começo sem erro nenhum, e `max_record_sec` (175 s) existe para parar antes disso.

## `wispr/stt.py`

```python
class TranscriptionError(RuntimeError): ...

class Engine:
    def __init__(self, cfg=None): ...
    backend: str          # "cuda" | "cpu" | "groq"
    model_id: str
    load_s: float
    warm_s: float
    def transcribe(self, audio: np.ndarray) -> str   # float32 mono 16 kHz em [-1,1]
    def close(self) -> None

def postprocess(text: str, fixups) -> str
```

`Engine.__init__` tenta CUDA, e se falhar registra o motivo e cai para CPU.
Com `engine="groq"` usa a API da Groq e nunca carrega modelo local.

## `wispr/inject.py`

```python
INJECT_TAG: int       # dwExtraInfo das teclas que nós mesmos injetamos
MSG_ELEVATED: str     # frase pronta para quem usar o módulo sozinho

class Delivery(str):  # é str, NUNCA dict: um isinstance(res, dict) dá False
    mode: str                     # "type" | "paste" | "empty"
    chars: int                    # code units UTF-16 CONFIRMADAS na janela
    target_elevated: bool | None  # cru: "a janela em foco é de admin?"
    blocked: bool | None          # acionável: "o UIPI vai engolir isto?"
    lost_nontext_formats: bool    # o restore só de texto matou uma imagem
    empty: bool                   # não havia texto depois do sanitize()
    def as_dict(self) -> dict
    def keys(self); def get(self, key, default=None)   # também indexa por nome

def deliver(text: str, cfg=None) -> Delivery   # "type" | "paste" | "empty"
def type_unicode(text: str, chunk_chars=64, gap=0.0005) -> int
def paste_text(text: str, restore=True, settle=0.06, restore_delay=0.25) -> dict
def tap(vk: int, mods=()) -> None
def clip_get_text() -> str | None
def clip_set_text(s: str) -> None
def sanitize(text: str) -> str
def target_is_elevated() -> bool | None
def injection_blocked() -> bool | None
```

**Os seis atributos do `Delivery` existem em TODO retorno de `deliver()`**,
inclusive nos retornos antecipados e nos caminhos de exceção — `deliver()` nunca
levanta. Ler por atributo, não por `isinstance(res, dict)`.

- `target_elevated` × `blocked`: `target_elevated` é a resposta **crua** ("a
  janela em foco é de administrador?") e sozinha não significa falha — se o
  wisper também estiver elevado a injeção passa normalmente e agir sobre ela é
  alarme falso. `blocked` é a resposta **acionável** (alvo elevado **e** nós
  não), e é ela que autoriza cair para o clipboard. Nos dois, `None` quer dizer
  "não deu para saber", nunca "não".
- `chars` conta **code units UTF-16 entregues**, acumuladas, nunca o tamanho do
  que se queria entregar. `chars > 0` com falha = entrega **parcial**: repetir o
  texto duplicaria o pedaço que já entrou, e quem chama não pode colar por cima.
  `chars == 0` é o único caso em que tentar outro caminho é seguro.
- `empty` (e `mode == "empty"`) marca o texto que morreu no `sanitize()` — um
  U+200B sozinho, por exemplo. É `chars == 0` **sem falha nenhuma**: mandar isso
  para o clipboard deixaria o usuário com a área de transferência vazia e um
  aviso de erro que não aconteceu.

`type_unicode()` devolve as code units entregues e, quando falha, pendura a mesma
contagem acumulada em `.sent` da `OSError`. `paste_text()` devolve
`{"pasted": bool, "restored": bool, "lost_nontext_formats": bool}` e só levanta
quando nem o clipboard foi escrito.

## `wispr/overlay.py`

```python
class Overlay:
    def __init__(self, cfg=None): ...
    def start(self) -> "Overlay"     # sobe a thread Tk, retorna self
    def show_recording(self, ) -> None
    def show_transcribing(self) -> None
    def show_message(self, text: str, ms: int = 1800) -> None
    def set_level(self, rms: float) -> None   # alimenta as barrinhas
    def hide(self) -> None
    def stop(self) -> None
```

A janela é `overrideredirect`, topmost, com transparentcolor, e recebe
`WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE`. Ela **nunca**
pode roubar o foco — se roubar, o texto é injetado na janela errada.

## `wispr/tray.py`

```python
class Tray:
    def __init__(self, app): ...     # app é a instância de wispr.app.App
    def run(self) -> None            # BLOQUEIA; tem que rodar na thread principal
    def stop(self) -> None
    def set_state(self, state: str) -> None   # "idle"|"recording"|"working"|"error"
    def notify(self, title: str, msg: str) -> None
```

`set_state()` aceita também os nomes da máquina de estados do app
(`transcribing`, `injecting`, `loading`, `paused`, ...): a bandeja traduz.

O estado guardado **não** é necessariamente o estado pintado. A cada repintura a
bandeja reavalia: `idle` com `HotkeyEngine.alive is False` vira vermelho (sem
hook não existe Win+A), e o vermelho volta ao estado real do app quando o hook
está vivo de novo, o microfone não está morto e o `App.state` é legível. Essa
descida é obrigatória: `App.on_hook_error()` não arma timer de volta ao verde e
conta com ela, senão o ícone fica vermelho para sempre depois que o watchdog
reinstala o hook.

Do `App` a bandeja lê, sempre com `getattr` e tolerando ausência: `state`,
`mic_ok`, `engine_status`, `status_text()`, `enabled`/`is_enabled()`,
`last_text`, `history`, os subsistemas `mic`, `hotkey` e `engine`, e as ações
`toggle_enabled`, `repaste_last`, `set_engine`, `open_config`, `open_logs` e
`shutdown`.

## `wispr/app.py` — o integrador

```python
class App:
    cfg: Config
    log: logging.Logger
    def __init__(self, cfg=None): ...
    def run(self) -> int            # sobe tudo e entra no loop do tray
    def shutdown(self) -> None
    # ações expostas ao tray
    def toggle_enabled(self) -> None
    def repaste_last(self) -> None
    def set_engine(self, name: str) -> None   # "local" | "groq", a quente; nunca levanta
    def open_config(self) -> None
    def open_logs(self) -> None
    # leitura para a bandeja (nenhuma levanta)
    state: str                      # "idle"|"recording"|"transcribing"|"injecting"
    enabled: bool                   # False = ditado pausado pelo usuário
    mic_ok: bool                    # o Mic existe E o stream está de pé
    engine_status: str              # "idle"|"loading"|"ready"|"error"
    def hook_alive(self) -> bool | None
    def status_text(self) -> str    # uma linha em pt-BR para tooltip/menu
    last_text: str
    history: list[str]
    # subsistemas, para quem precisa de diagnóstico ao vivo
    mic: "audio.Mic | None"
    engine: "stt.Engine | None"
    hotkey: "hotkey.HotkeyEngine | None"
    overlay: "overlay.Overlay"
```

Máquina de estados: `idle -> recording -> transcribing -> injecting -> idle`.
`Esc` volta para `idle` sem injetar nada.

`set_engine()` compara com o **backend vivo** (`engine.backend`), nunca com
`cfg["engine"]`: a bandeja grava a chave nova antes de chamar, então uma
checagem pela config nunca trocaria nada. Uma troca que falha deixa o estado em
`error`, avisa o usuário e o próximo ditado tenta de novo.
