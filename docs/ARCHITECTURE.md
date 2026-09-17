# wisper — arquitetura e decisões

Clone do WisprFlow para Windows 11. Roda em segundo plano, `Win+A` começa a gravar,
`Enter` finaliza e digita o texto na janela em foco, `Esc` cancela.

Tudo neste documento foi **medido nesta máquina** (Windows 11 Pro 26200, RTX 4060 Ti,
Logitech PRO X, teclado US + ABNT2). Não é chute de documentação.

---

## 1. Decisão central: a assinatura ChatGPT Plus NÃO transcreve áudio

Pedido original: usar os tokens da conta Plus/Codex em vez de API key da OpenAI.
**Isso é tecnicamente impossível.** Provado, não suposto:

- `codex 0.147.0-alpha.6.5` tem um subsistema realtime completo (`thread/realtime/start`,
  `RealtimeWsMode conversational|transcription`, `gpt-realtime-1.5`, `QuicksilverRealtimeTranscription`
  com `audio/pcm` + `server_vad`). Percorremos toda a cadeia de gates — feature flag
  `realtime_conversation`, capability `experimentalApi`, campo `outputModality` — e o próprio
  binário responde: **"realtime conversation requires API key auth"**. Nas 3 versões (v1, v2, v3).
- O caminho não-realtime (`turn/start` com `localAudio`) é aceito pelo schema, chega ao modelo
  como `userMessage` e volta `SEM_AUDIO`.
- O catálogo de modelos da conta (cache em disco e `model/list` ao vivo) só tem modelos de
  texto+imagem. O `access_token` OAuth tem `scp: [openid, profile, email, offline_access]`,
  `aud: https://api.openai.com/v1`, `plan: plus`. Não existe feature flag de áudio em
  `codex features list`.
- Como caminho de **texto** o `codex` funciona, mas custa 2,2–3,0 s por turno com app-server
  quente (3,6–6,7 s com `codex exec` frio) e ~12–15k tokens de input por chamada trivial.
  4 chamadas de teste levaram a janela de quota de 5 h de 6% para 17%.

> Raspar `chatgpt.com/backend-api/transcribe` com token de sessão funcionaria tecnicamente e
> está **fora de questão**: viola os Termos de Uso da OpenAI (extração programática fora da API).

**Consequência:** o STT é **local**. A conta Plus só entra, se você quiser, num comando manual
opcional de "polir transcrição" — nunca no caminho quente.

## 2. STT: faster-whisper local na 4060 Ti

- Modelo: `deepdml/faster-whisper-large-v3-turbo-ct2`, `compute_type="int8_float16"`.
- 1024 MB de VRAM, RTF 0,049 (~20x tempo real), 0,40–0,62 s por fala de 6–15 s.
- WER ~10,3% em pt-BR ruidoso com jargão de dev; ~8,5% com `initial_prompt`.
- `initial_prompt` **sem acentos** é o maior ganho de acurácia medido (13,4% → 8,5%).
  A versão com acentos só chega a 9,8%. Prompt longo (368 tokens) ficou **pior que nenhum**:
  `get_prompt` trunca em `max_length//2` = 224 tokens. Manter curto.
- `hotwords` é pior que o prompt **e cancela o benefício dele** quando combinados. Não usar.
- `beam_size=1` custa no máximo 1,2pp de WER e economiza ~0,3 s.
- **A escada de temperatura fica DESLIGADA** (`temperature=[0.0]`, greedy puro). Num ditado
  real cabem poucas palavras dentro da janela de 30 s, o `avg_logprob` do decode greedy cai
  abaixo do `log_prob_threshold` (-1,0) e o faster-whisper marca `needs_fallback` e passa a
  **sortear** até temperatura 1,0 — depois escolhe por `avg_logprob` entre amostras de
  temperaturas diferentes, que não são comparáveis entre si (`generate_with_fallback`,
  transcribe.py:1479-1530). Foi de lá que saíram as corridas de pontuação do primeiro ditado
  real em hardware (`Oi,` seguido de 51 pontos, `Faça deploy` seguido de 10). Os outros
  guarda-chuvas não pegam: a razão de compressão de uma corrida curta dá ~1,0 contra um limiar
  de 2,4, e `no_speech_prob` volta 0,0 porque há fala de verdade no clipe. Com um único degrau
  o mesmo áudio dá sempre o mesmo texto — o `ctranslate2` não tem semente fixada aqui.
  `temperature_fallback: true` no config.json devolve o comportamento original.
- O `postprocess()` ainda tem um cinto contra corrida de pontuação (`strip_punct_runs`), e ele
  é deliberadamente conservador: nunca remove letra nem dígito, só colapsa pontuação repetida e
  corta cauda de pontos SOLTA ou EMPILHADA em outra pontuação. O preço aceito é a reticência
  ditada que chega separada por espaço ("e depois ..." vira "e depois"), que no `" ".join()`
  dos segmentos quase sempre é segmento de alucinação, não fala.
- Modelo resolvido no cache local antes de carregar: `download_model(id, local_files_only=True)`
  devolve o diretório do snapshot, e um **diretório** faz o `WhisperModel` pular o Hub. Sem
  isso ele bate em `huggingface.co/api/models/<id>/revision/main` a **cada** boot (visto no log
  do bring-up às 00:08:57). Snapshot sem `model.bin`, `config.json` ou `tokenizer.json` conta
  como incompleto e cai no caminho online, que se repara sozinho — `tokenizer.json` ausente não
  é erro no faster-whisper, ele busca o tokenizer do `openai/whisper-tiny` pela rede, e com
  vocabulário errado para o large-v3. `preprocessor_config.json` não entra nessa lista: o
  `faster-whisper-small` não tem esse arquivo e está correto, porque 80 mel bins é o default.
- **Nunca** usar `Systran/faster-distil-whisper-large-v3`: ele traduz pt-BR para inglês
  silenciosamente (WER 73–106%).
- Armadilha Windows: `ctranslate2.dll` carrega `cublas64_12.dll` **dinamicamente** (não está na
  import table do PE). O modelo carrega em `cuda` e só o `transcribe()` estoura com
  `Library cublas64_12.dll is not found`. A correção precisa de **duas** partes:
  `os.add_dll_directory()` nos `nvidia/*/bin` das wheels **e** os mesmos diretórios no
  `os.environ["PATH"]`, ambos **antes** de importar `faster_whisper`. `nvidia` é namespace
  package, então usar `nvidia.__path__`, nunca `nvidia.__file__` (que é `None`).
- **O `add_dll_directory` sozinho não resolve** — isto foi medido no bring-up, contra o que uma
  sessão anterior tinha concluído. Ele só afeta quem chama `LoadLibraryEx` com
  `LOAD_LIBRARY_SEARCH_USER_DIRS`; o `ctypes` faz isso (`ctypes.WinDLL('cublas64_12.dll')`
  carregava sem problema), mas o `ctranslate2.dll` usa `LoadLibrary` simples, que ignora esses
  diretórios e cai na ordem de busca clássica — onde o `PATH` entra. O sintoma é traiçoeiro:
  o `Engine` cai para CPU **em silêncio**, porque o fallback para CPU é exatamente o
  comportamento correto quando falta VRAM. Só o campo `backend` denuncia. Custo real medido:
  a mesma fala de 15,1 s levou 5,18 s na CPU contra 0,97 s na GPU.
  Travado por `tests/test_regressions.py::CudaDllPathTest`.
- VRAM livre real com o desktop em uso (Wallpaper Engine, Opera GX, WhatsApp, WebView) foi de
  apenas **1.746 MiB de 8.188**. Orçar ~1,5 GB, não 6,5 GB. Daí o `int8_float16`.

### Fallback opcional na nuvem
Groq `whisper-large-v3-turbo` (`https://api.groq.com/openai/v1/audio/transcriptions`).
Tier gratuito: 20 RPM / 2.000 req-dia / 28.800 segundos de áudio por dia — cobre folgado
90 min/dia de ditado a custo zero. Pago seria US$ 0,04/hora, cerca de US$ 0,88/mês a 60 min/dia.
Segunda opção: Cloudflare Workers AI `@cf/openai/whisper-large-v3-turbo`.

## 3. Hotkey Win+A: só com hook de baixo nível

- `RegisterHotKey(MOD_WIN, 'A')` retorna 0 com **erro 1409** (`ERROR_HOTKEY_ALREADY_REGISTERED`).
  O shell do Windows 11 é dono de todos os `Win+letra`. Não dá nem para registrar.
- Único caminho: `WH_KEYBOARD_LL` via `ctypes`, retornando `1` para engolir o evento.
  Verificado: a Central de Ações **nunca** abre, desde que você engula o **keydown E o keyup**
  do A e injete uma tecla-máscara (`vk 0xE8`) para desarmar o menu Iniciar no keyup do Win.
  Sem a máscara, o painel "Pesquisar" (`Windows.UI.Core.CoreWindow`) aparece.
- Custo medido do callback: 0,017 ms médio / 0,051 ms máximo, contra um orçamento de 300 ms
  (`LowLevelHooksTimeout` não está setado nesta máquina, então vale o default). O hook proc só
  pode setar flags e chamar `queue.put()`; qualquer I/O nele faz o Windows despejar o hook
  silenciosamente.
- Toda tecla que **nós** injetamos leva uma tag própria em `dwExtraInfo`, e o hook ignora
  eventos com essa tag — senão o app se retriggera.
- `k32.GetModuleHandleW.restype = w.HMODULE` é obrigatório: sem isso o handle de 64 bits
  trunca e `SetWindowsHookExW` falha com erro 126.
- A biblioteca `keyboard` do PyPI está **descartada**: casa hotkey por scan code, não dispara
  em input injetado com scancode 0, não tem masking da tecla Win, e
  `key_to_scan_codes('ç')` levanta `ValueError` — inviável para pt-BR.
- **Buraco conhecido (UIPI):** com uma janela elevada em foco (Gerenciador de Tarefas como
  admin) o hook recebe **0 eventos** e o Win+A vaza para a Central de Ações. Só rodar elevado
  resolve. `schtasks /Create` falha com "Acesso negado" a partir de shell não elevado mesmo
  sem `/RL HIGHEST` — a criação da tarefa precisa de uma passada com UAC.
- `Ctrl+Alt+A` é aceito normalmente por `RegisterHotKey` — fica como chord alternativo.

## 4. Áudio: WASAPI compartilhado, stream sob demanda

- **Desde 2026-09-17 o stream só existe do `Win+A` ao `Enter`/`Esc`** (`mic_on_demand: true`,
  padrão). Motivo, visto no segundo PC: o Windows mostrava "Microfone em uso por Python" o tempo
  todo, e um headset Bluetooth fica preso no perfil mãos-livres (música com qualidade de
  telefone) enquanto qualquer app segura o microfone. O que muda: `Mic.start()` abre, `Mic.stop()`
  fecha, o supervisor só reabre enquanto `_wanted` está ligado, e o `mark()` tem um piso no
  `start()` para nunca rebobinar para dentro do ditado anterior que ainda está no ring.
  O que se perde: o **pré-roll** de 0,35 s — não havia captura antes da tecla. A abertura mede
  ~9 ms no WASAPI quente; em Bluetooth HFP pode ser bem mais, e aí a primeira sílaba pode sumir.
  `"mic_on_demand": false` devolve o stream permanente. Tudo abaixo vale para os dois modos.

- **Interpretador:** python.org CPython 3.11.9, **não** o da Microsoft Store. O Store Python
  tem identidade de pacote (`PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0`) com entrada
  própria no ConsentStore do microfone: em "Prompt" o `InputStream()` **trava para sempre sem
  exceção** esperando uma UI de consentimento que nunca aparece; depois vira "Deny" e falha com
  `-9999/-9996` em todo device e host API. Ele também **redireciona silenciosamente** toda
  escrita em `%LOCALAPPDATA%` e `%APPDATA%` para o sandbox do pacote. `%USERPROFILE%`, `%TEMP%`
  e `D:\` não são redirecionados.
- WASAPI compartilhado aceita **só 48000 Hz** neste endpoint (confirmado pelo blob do registro
  em HKLM MMDevices: `WAVE_FORMAT_EXTENSIBLE`, 1 canal, 48 kHz, 16 bits).
- Latência de abertura: WASAPI 9,4 ms até o primeiro callback, contra 77,7 ms no MME e
  53,3 ms no DirectSound. Fria: 80–92 ms; quente: ~8,6 ms, sem decair em 45 s ocioso.
  O stream permanentemente aberto se justifica pelo **pré-roll**, não pela latência.
- Resolver o device por **COM**, nunca pelo PortAudio:
  `IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture, eCommunications)` então
  `OpenPropertyStore(0)` e `PKEY_Device_FriendlyName`, e casar esse nome contra
  `sd.query_devices()` filtrado por host API `Windows WASAPI`. `sd.default.device` aponta para
  o clone MME de 90 ms, e o `default_input_device` do PortAudio ignora o papel `eCommunications`.
- Trocas de headset: `RegisterEndpointNotificationCallback` com `IMMNotificationClient`
  (5,2 ms de latência, 0,0000% de CPU ociosa) — melhor que qualquer polling.
- `sys.coinit_flags = 0` e importar `comtypes` **antes** de `sounddevice`: importar
  `sounddevice` roda `Pa_Initialize`, cujo backend WASAPI chama
  `CoInitializeEx(COINIT_APARTMENTTHREADED)`, e aí o import do `comtypes` levanta
  `RPC_E_CHANGED_MODE`.
- **Silêncio digital:** este headset sem fio às vezes entrega *todas as amostras exatamente 0.0*
  enquanto o Windows reporta `state=Active`, não mudo, volume 0,85, `stream.active=True`,
  callbacks no horário, zero xruns — e o próprio medidor `IAudioMeterInformation` lê 0.0.
  **Nenhuma API reporta erro.** Só um teste de RMS no áudio devolvido detecta isso.
  Daí o `SILENCE_RMS = 3e-5` obrigatório por gravação.
- **Modo exclusivo alheio mata nosso stream:** uma abertura WASAPI exclusiva falha de outro
  processo derruba o nosso stream compartilhado (`active` vira False, frames param, nenhuma
  flag de status). Supervisor de 0,5 s em `stream.active` mais avanço de frames recuperou em
  0,75 s via `abort/close`, `sd._terminate()/sd._initialize()`, reabrir.
- DSP: `soxr.resample(quality='HQ')` 48k para 16k (0,68 ms por 5 s, rejeição de alias -156 dB,
  contra -73,6 dB do `scipy.resample_poly`), depois Butterworth 4ª ordem passa-alta em 80 Hz
  via `sosfilt` (causal está correto: o log-mel do Whisper é só magnitude, ignora fase), depois
  normalização de pico para -3 dBFS **guardada por `if peak > 1e-5`** para nunca amplificar
  silêncio puro. Whisper não é invariante a ganho: as features deslocam 0,5 por década.
- VAD: `webrtcvad-wheels` em agressividade 2 ou 3 com hangover de 10 frames (300 ms) em frames
  de 30 ms/16 kHz. Modo 1 vazou 5,4% de falso-positivo em ruído de sala real.

## 5. Injeção de texto

- `SendInput` com `KEYEVENTF_UNICODE` é **independente de layout** — verificado nos três layouts
  desta máquina (en-US, ABNT2 `0416:00000416`, pt-BR US-Intl `0416:00020409`), sem composição
  de dead key e com acentuação pt-BR perfeita em ida e volta.
- Layout x64 de `INPUT`: `sizeof == 40`, `struct` format `"<I4xHHII4xQ8x"` — byte-idêntico às
  structs do ctypes.
- Velocidade: `SendInput` cru faz 37–44k chars/s, mas a latência **visível** é ~1,7 ms/char
  (250 chars = 0,55 s; 5000 chars = 10,1 s). Colar pelo clipboard é plano: ~60 ms para enviar,
  ~20 ms para aparecer, em **qualquer** tamanho. Daí o híbrido: digita até ~120 chars, cola acima.
- Restaurar o clipboard exige atraso: com 0 ms o app cola o conteúdo **antigo**. 20 ms foi o
  mínimo que funcionou; o padrão do módulo é 250 ms.
- Uma imagem `CF_DIB` no clipboard é destruída por um restore só de `CF_UNICODETEXT`. O módulo
  avisa quando isso aconteceria.
- **UIPI de novo:** dentro de janela elevada, tanto o `SendInput` quanto o Ctrl+V são
  descartados **silenciosamente**, com `GetLastError() == 0`.
- Liberar modificadores presos antes de injetar (Shift/Ctrl/Alt/Win) evita texto corrompido.

## 6. Processos e threads

Quatro threads, validadas juntas num teste de integração que passou:

| thread | dono de | observação |
|---|---|---|
| principal | `pystray` (`icon.run()`) | tem seu próprio bombeamento Win32 |
| hook | `WH_KEYBOARD_LL` + `GetMessageW` | dedicada; só enfileira eventos |
| overlay | pílula `tkinter` | `WS_EX_LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE` = `0x080800A8`, revelada com `ShowWindow(SW_SHOWNOACTIVATE)` |
| worker | captura, ASR e injeção | onde todo o trabalho pesado acontece |

- Instância única: `CreateMutexW(None, True, "Local\\WisprClone")` mais `ERROR_ALREADY_EXISTS (183)`.
- Watchdog do hook: input recente no SO (`GetLastInputInfo`) com o hook proc calado há mais de
  5 s é **suspeita, não diagnóstico**. `GetLastInputInfo` conta teclado **e mouse**, e um
  `WH_KEYBOARD_LL` jamais dispara em evento de mouse: cinco segundos de mouse sem teclado batem
  nos dois limites com o hook perfeitamente vivo. Na sessão de 2 min do primeiro ditado real
  isso deu duas reinstalações (00:09:02 e 00:10:47) com o `Win+A` funcionando entre elas. Quem
  dá o veredito é uma **sonda**: um toque de `vk 0xE8` com tag própria (`PROBE_TAG`), que o
  hook proc engole antes de qualquer lógica de chord. Só sonda perdida reinstala. Com janela
  elevada em foco (o UIPI engole a injeção **e** o hook não recebe nada de qualquer jeito) ou
  com `SendInput` recusado, o veredito é "não dá para saber" e nada é reinstalado — reinstalar
  ali não devolveria evento nenhum. O relógio zera a cada veredito, então a sonda roda no
  máximo a cada 5 s, nunca a cada ronda de 3 s.
- Autostart: entrada em `HKCU\...\CurrentVersion\Run` apontando para `pythonw.exe main.pyw`
  (gravável sem elevação, verificado). Tarefa agendada com `/RL HIGHEST /SC ONLOGON` só se você
  quiser que o hook sobreviva sobre janelas elevadas — precisa de uma criação elevada única.
- PyInstaller fica para depois. v1 roda do fonte.

## 7. Correções a crenças anteriores

- WisprFlow **não** está instalado nesta máquina. Existe um marcador Squirrel `.dead`, só o
  `squirrel.exe` sobrou e há uma entrada órfã em `HKCU Run` apontando para um exe que não existe.
  (Uma sessão anterior afirmou que estava instalado na v1.4.57 — estava errada.)
- O UX real do WisprFlow, para referência: push-to-talk `Ctrl+Win`, toggle `Ctrl+Win+Space`,
  `Esc` cancela, pílula flutuante com barras brancas mais ping sonoro, e inserção por
  clipboard e `Ctrl+V` de uma vez (nunca em streaming).
