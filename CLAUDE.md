# wisper — onde paramos

Clone do WisprFlow para Windows 11. `Win+A` grava, `Enter` finaliza e digita o texto na janela
em foco, `Esc` cancela. STT local com faster-whisper na GPU.

**Estado: funciona de ponta a ponta, com um bug aberto (reticências).** O bring-up em hardware foi feito
em 2026-09-17 na máquina original (Windows 11 Pro 26200, RTX 4060 Ti, Logitech PRO X).

Leia `docs/ARCHITECTURE.md` antes de mexer em qualquer coisa. Tudo lá foi **medido**, não é
documentação copiada. `docs/CONTRACT.md` tem a API entre os módulos.

---

## Regras desta máquina

1. **Nunca rode nada que sequestre teclado, mouse, clipboard ou microfone sem o usuário pedir
   naquele momento.** Ele usa a máquina enquanto você trabalha. Uma sessão anterior instalou
   hooks de teclado e injetou teclas enquanto ele jogava e atrapalhou tudo. Se você delegar para
   subagentes, **repita a regra dentro do prompt de cada um** — eles não herdam isso.
2. O que é seguro rodar sozinho: `python -m py_compile`, e a suíte offline
   `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` (359 testes).
   `tests/_safety.py` transforma `SendInput`, `SetWindowsHookExW` e `OpenClipboard` em bomba de
   `RuntimeError`, então a suíte é segura mesmo com alguém jogando.
3. **Não lance o app de dentro de uma chamada de ferramenta comum** — o harness mata a árvore de
   processos quando a chamada termina, e o app morre junto sem deixar rastro. Use
   `run_in_background`. Isso já custou um ciclo de diagnóstico inteiro.

## Setup num PC novo

O repo não carrega o `.venv` (2,4 GB) nem `models/` (2,5 GB). Num PC novo:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -DownloadModel
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Console
```

O `setup.ps1` **recusa o Python da Microsoft Store** e está certo em recusar: aquele build tem
identidade de pacote, e o `InputStream()` do microfone trava para sempre sem exceção nenhuma.
Use python.org CPython 3.11 x64. Passe `-Python <caminho>` se não estiver no lugar padrão.

Se o PC novo não tiver GPU NVIDIA, o `Engine` cai para CPU sozinho — funciona, mas fica ~6x mais
lento (RTF 0,34 contra 0,055). Para nuvem em vez de CPU: `"engine": "groq"` no `config.json` mais
`GROQ_API_KEY` no ambiente ou no `.env`. Com `engine: auto` (padrão) isso acontece sozinho quando
a CUDA falha. Rodou ao vivo no notebook em 2026-09-17: 0,5 s por ditado.

**Segundo PC (notebook i5-13420H, Intel UHD, headset Bluetooth JBL TUNE125TWS), 2026-09-17:**
- Setup limpo funcionou de primeira. CUDA cai para CPU com `CUDA driver version is insufficient`
  (não há driver NVIDIA), load 11 s, warm 8 s, RTF **0,58–1,4** nos fixtures. Bem pior que o 0,34
  do desktop: ditado de 10 s leva de 6 a 14 s para virar texto.
- O headset Bluetooth em mãos-livres (HFP) **só abre em 16 kHz** e responde `-9997 Invalid sample
  rate` a 48 kHz. O `Mic._open` agora reabre na taxa nativa do endpoint quando a configurada é
  recusada. `capture_sr` continua 48000 no config: é só o primeiro palpite.
- **Microfone sob demanda** (`mic_on_demand: true`, padrão): o stream só existe do `Win+A` ao
  `Enter`/`Esc`. O usuário viu "Microfone em uso por Python" o tempo todo e pediu isso. Custo: sem
  pré-roll. `docs/ARCHITECTURE.md` seção 4 tem o desenho (`start()`/`stop()`, piso do `mark()`,
  trava `_wanted` no supervisor). Falta confirmar ao vivo que o ícone de microfone some entre
  ditados e que a primeira sílaba não é cortada.
- **Pílula = Flow Bar do Wispr Flow**, copiada do vídeo em wisprflow.ai: preta, contorno claro
  fino, 11 barras brancas de ponta redonda que descansam como pontos, 100x36. Transcrevendo é uma
  onda varrendo as barras (sem texto, sem spinner). Entre ditados fica um traço cinza de 44x8 no
  rodapé (`overlay_idle_pill: false` tira). Constantes no topo de `wispr/overlay.py`. Conferido
  com captura de tela dos quatro modos, não no ditado real.
- A sonda de liveness do hook rodou aqui em DEBUG e disse `the hook is alive` — nesta máquina a
  cadeia de hooks não é problema.
- **Velocidade na CPU: o custo é FIXO, 6,5–8 s por ditado, tanto para 4 s quanto para 12 s de
  áudio.** É o encoder sobre a janela de 30 s. Tudo que foi medido e **não** ajuda (não repita):
  `cpu_threads` 4/6/8/12 (o padrão 0 é o melhor; 4 só-P-cores é pior), `int16` (2x pior),
  `int8_float32` (igual), `chunk_length` 20/15/10/6 (o CTranslate2 completa para 30 s por dentro:
  tempo igual, WER piora, 6 alucina). Modelos: `small` 2,4 s mas WER 38% (79% com ruído);
  `medium` mesma qualidade do turbo e quase o mesmo tempo. ISA: AVX2 só, sem AVX512/VNNI.
  **Neste notebook a única saída para ~1 s é o Groq.** E é o que roda aqui: `engine: auto`
  (padrão) cai para o Groq quando a CUDA falha e há `GROQ_API_KEY` (lida do `.env` da raiz pelo
  `config.load_dotenv()`). **Rodou ao vivo em 2026-09-17:** 0,52 s para 3,8 s de fala. O primeiro
  ditado deu 403 do Cloudflare (erro 1010, User-Agent `Python-urllib`); corrigido com User-Agent
  próprio. No desktop o mesmo config fica na 4060 porque a CUDA carrega.

## O que está provado em hardware

Medido, não inferido:

| | |
|---|---|
| `Win+A` | engole a tecla, a Central de Ações **não** abre |
| pílula | `exstyle=0x080800A8`, DPI per-monitor-v2, não rouba foco |
| bandeja | pystray na thread principal convive com as outras 3 threads |
| microfone | resolução por COM acha o endpoint certo (WASAPI 48 kHz, device 21, **não** o clone MME de 90 ms), abre em 34 ms |
| STT | `backend=cuda`, load 3,0 s, warm 0,45 s, RTF 0,025–0,045, **WER 3,6%** nos fixtures |
| injeção | `SendInput` unicode com acento correto; `chars=65/65` (contabilidade de entrega parcial certa) |
| shutdown | fecha engine, overlay e bandeja sem travar |
| ditado completo | `Win+A` → fala → `Enter` → texto no Bloco de Notas |

## BUGS

### 1. Reticências no texto entregue — CORRIGIDO no código (2026-09-17), falta confirmar ao vivo

**Sintoma real, com o app rodando:** o usuário ditou três vezes e recebeu no Bloco de Notas
`asasAlô,....................`, depois uma linha inteira de pontos, depois `Isso de`.

**Causa raiz (duas):** a escada de temperatura, já desligada (`GREEDY_ONLY`, decode determinístico
e 2,6x mais rápido, manter), e o decoder destilado do turbo entrando em loop de `.` quando a
janela de 30 s tem pouca fala. O cinto `strip_punct_runs` existia, mas vazava por dois furos:

1. Rodava a regra de cauda **empilhada** (`Oi,...` → `Oi,`) **antes** da cauda **solta**. Com
   vários grupos de pontos (`Alô,.... ....`) a solta corta só a partir do espaço e sobra
   `Alô,...`, que é exatamente o caso da empilhada — que já tinha passado. Agora a ordem é
   colapso → solta → empilhada.
2. Um segmento só de pontos **no meio** da lista (`["Alô,", "....", "Isso de"]`) nunca é cauda.
   `_transcribe_local` agora descarta, antes do join, qualquer segmento sem letra ou dígito; e
   `postprocess()` devolve `""` para texto sem nenhuma letra ou dígito (não é censura: não há o
   que censurar). O caminho `groq` passa pelo mesmo `postprocess`.

Sete testes em `tests/test_regressions.py` (`PunctuationRunGuardTest`), três mutantes remutados
em memória, todos vermelhos. **WER idêntico antes e depois** (0,0 / 10,7 / 0,0; média 3,6%),
medido com `tests/bench_wer.py` — não é `test_*` de propósito, ele carrega o modelo.

**Ao vivo no segundo PC (10:21–10:24), COM a correção rodando, ainda vazou:** o Bloco de Notas
recebeu `Alô,ssssssssssssssssssssso ........................` (52 chars) e `Test your ....`
(14 chars). A correção prova que `'Test your ....'` (4 pontos ASCII) vira `'Test your'` — logo o
texto real tem outra forma que a tela não revela (14 chars cabe em `'Test your. . .'`: ponto
colado na palavra mais dois soltos, que a cauda solta não conta). Ambiente ruim: mic Bluetooth
"podre", muito barulho, o modelo loopou em `sss` e saiu em inglês. **Não adivinhe a forma:**
`config.json` já está com `log_level: DEBUG` e `keep_recordings: true`, e o `_transcribe_local`
loga em DEBUG `stt segments=[...] -> '...'` com o texto exato. Peça um ditado, leia o log, e só
então ajuste o `strip_punct_runs`. Os wavs ficam em `logs/recordings/`.

### 2. `config.json` com BOM era ignorado inteiro, em silêncio — CORRIGIDO (2026-09-17)

### 2. `config.json` com BOM era ignorado inteiro, em silêncio — CORRIGIDO (2026-09-17)

O `Out-File` do PowerShell e o Bloco de Notas gravam UTF-8 **com BOM**, o `json.loads` estourava
e o `except` engolia: o arquivo inteiro voltava aos defaults sem aviso. Custou um ciclo de teste
(`{"log_level": "DEBUG"}` nunca ligou o DEBUG).

Agora `config.load()` lê com `utf-8-sig`, guarda o motivo de qualquer falha em
`config.load_error`, e o `App.run()` reloga isso como WARNING depois de o logging subir (o
primeiro `load()` roda antes do `logging_setup.setup()`, então logar só dentro do `load()` se
perdia sob `pythonw`). Testes em `tests/test_config.py`, verificados por mutação.

## Suspeita não confirmada: ordem da cadeia de hooks

Num dos testes o `Win+A` só passou a funcionar **depois** de o watchdog reinstalar o hook, e num
outro não funcionou enquanto nenhuma reinstalação aconteceu. A hipótese é que outro processo
(G HUB, Wallpaper Engine, overlay de jogo) tenha um `WH_KEYBOARD_LL` na frente do nosso comendo a
tecla, e que reinstalar nos jogue para a frente da cadeia.

**Não está confirmado, e no último teste o `Win+A` funcionou sem nenhuma reinstalação** — então
pode ter sido outra coisa. Se voltar a acontecer, o jeito de separar é o que preparamos e não
chegamos a rodar: com o app em DEBUG, deixe o teclado parado por mais de
5 s mexendo só o mouse. Isso força a sonda de liveness, e o log diz uma de três coisas:
`the hook is alive` (hook OK, a tecla some antes de chegar), `probe never reached the hook proc`
(hook morreu mesmo) ou `the liveness probe cannot run now` (a sonda não pôde rodar — aí a
correção do watchdog trocou um falso positivo barulhento por um falso negativo mudo, e é ela que
tem que mudar).

O watchdog **antes** reinstalava toda hora sem motivo, porque `GetLastInputInfo` conta **mouse**
e um hook de teclado nunca dispara com mouse. Isso está corrigido (sonda positiva com `VK 0xE8`
marcado com `PROBE_TAG`, ver `_probe_hook` em `wispr/hotkey.py`).

## Coisas que decidimos e é melhor não refazer

- **A conta ChatGPT Plus não transcreve áudio.** Isso foi provado até o binário do Codex
  responder `"realtime conversation requires API key auth"` nas três versões de realtime.
  Não tente de novo; `docs/ARCHITECTURE.md` seção 1 tem a cadeia inteira de gates.
- **Nunca** `Systran/faster-distil-whisper-large-v3`: traduz pt-BR para inglês em silêncio
  (WER 73–106%).
- O `initial_prompt` fica **sem acentos** de propósito (WER 8,5% contra 9,8% com acento) e curto
  (`get_prompt` trunca em 224 tokens; prompt longo ficou pior que nenhum). `hotwords` é pior que
  o prompt **e** cancela o benefício dele.
- As DLLs da CUDA precisam de `os.add_dll_directory()` **e** do `%PATH%`. Só o primeiro não
  resolve: o `ctranslate2.dll` usa `LoadLibrary` simples. Sem isso o app cai para CPU **em
  silêncio**, porque cair para CPU é o comportamento correto quando falta VRAM.
- Rodar não-elevado deixa um buraco sem conserto: com janela de administrador em foco o hook
  recebe **zero** eventos e o `SendInput` é descartado com `GetLastError() == 0`.
  `scripts\autostart_install.ps1 -Task` resolve, ao custo de um UAC na instalação.

## Como está o repositório

- `main` em `https://github.com/danieltanjos/wisper` (privado).
- 359 testes offline, todos verificados por mutação — cada correção foi remutada em memória para
  provar que o teste fica vermelho sem ela. Mantenha esse padrão: a suíte anterior tinha 180
  testes verdes e **não pegou nenhum** dos defeitos que o hardware achou.
- Três auditorias adversariais (contrato, concorrência, modos de falha) mais duas rodadas de
  correção estão no histórico. 28 defeitos corrigidos antes do primeiro boot.
- O que o hardware achou e nenhum teste acharia: CUDA caindo para CPU em silêncio, as
  reticências, e o watchdog reinstalando por causa do mouse. Ligar a coisa continua valendo mais
  que qualquer auditoria.
