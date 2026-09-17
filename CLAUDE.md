# wisper — onde paramos

Clone do WisprFlow para Windows 11. `Win+A` grava, `Enter` finaliza e digita o texto na janela
em foco, `Esc` cancela. STT local com faster-whisper na GPU.

**Estado: funciona de ponta a ponta, com dois bugs abertos.** O bring-up em hardware foi feito
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
   `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` (338 testes).
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
`GROQ_API_KEY` no ambiente. **Esse caminho nunca rodou ao vivo**, só compila e tem teste.

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

## BUGS ABERTOS

### 1. Reticências no texto entregue (o que estávamos caçando)

**Sintoma real, com o app rodando:** o usuário ditou três vezes e recebeu no Bloco de Notas
`asasAlô,....................`, depois uma linha inteira de pontos, depois `Isso de`.
Sessões no log: 65 chars e 80 chars, ambas `mode=type`.

**O que já foi feito (e não bastou):**
- A escada de temperatura foi desligada (`temperature=[0.0]`, `GREEDY_ONLY`). A causa raiz medida
  é real — `generate_with_fallback` (faster_whisper/transcribe.py:1479-1530) passa a **sortear**
  até T=1,0 quando o `avg_logprob` do greedy cai abaixo de -1,0, o que acontece sempre que um
  ditado curto deixa a janela de 30 s quase vazia, e depois compara amostras de temperaturas
  diferentes por um score que não é comparável entre elas. Isso também deixou o decode
  determinístico e **2,6x mais rápido**, então vale manter de qualquer forma.
- Um guard conservador em `postprocess()` (`strip_punct_runs`, em `wispr/stt.py`).

**Por que ainda vaza — o diagnóstico que faltava:** o guard limpa **uma** cauda de pontos, mas o
`_transcribe_local` junta os segmentos com `" ".join()`, e o modelo devolve **vários** grupos de
pontos. Verificado chamando a função direto:

```
'Alô,....................'                   -> 'Alô,'      OK
'Alô,.................... ...............'   -> 'Alô,...'   SOBRA
'........................................'   -> '...'       SOBRA
```

**Por onde começar amanhã:**
- O caminho mais promissor é **antes do join**: descartar o segmento inteiro quando ele não tem
  nenhuma letra ou dígito, em `_transcribe_local`, em vez de tentar limpar a string colada.
  Isso mata o caso de vários grupos de uma vez e é mais fácil de provar seguro.
- Vale olhar de novo `no_speech_threshold` e `log_prob_threshold` por segmento: nos casos
  medidos o `no_speech_prob` voltou 0,000 porque **há** fala real no clipe — o segmento de
  pontos é um segmento *adicional*, não o clipe inteiro.
- `logs/recordings/*.wav` tem gravações reais salvas (ligue `"keep_recordings": true`). Dá para
  iterar offline, sem pedir nada ao usuário. Os controles limpos de 8 s estão lá.
- **Meça o WER dos 3 fixtures antes e depois de qualquer mudança de decode.** Baseline atual:
  3,6% médio, `ptbr_short` e `ptbr_dev2` em 0,0%. Se regredir, volte atrás.

### 2. `config.json` com BOM é ignorado inteiro, em silêncio

`config.load()` em `wispr/config.py` faz
`json.loads(path.read_text(encoding="utf-8"))`. O `Out-File` do PowerShell e o Bloco de Notas
gravam UTF-8 **com BOM** (`EF BB BF`), o `json.loads` estoura com "Expecting value: line 1
column 1", o `except (OSError, ValueError)` engole, e o arquivo **inteiro** volta para os
defaults sem nenhum aviso.

Descoberto na prática: escrevemos `{"log_level": "DEBUG"}` para diagnosticar o bug 1 e o DEBUG
nunca ligou — o que custou um ciclo de teste com o usuário.

Correção: ler com `encoding="utf-8-sig"` (aceita com e sem BOM) **e** logar um WARNING quando o
parse falhar, em vez de cair calado nos defaults. Tem teste em
`tests/test_config.py` para "json corrompido cai nos defaults" — ele passa, e passaria também
com a correção; adicione um caso específico de BOM.

## Suspeita não confirmada: ordem da cadeia de hooks

Num dos testes o `Win+A` só passou a funcionar **depois** de o watchdog reinstalar o hook, e num
outro não funcionou enquanto nenhuma reinstalação aconteceu. A hipótese é que outro processo
(G HUB, Wallpaper Engine, overlay de jogo) tenha um `WH_KEYBOARD_LL` na frente do nosso comendo a
tecla, e que reinstalar nos jogue para a frente da cadeia.

**Não está confirmado, e no último teste o `Win+A` funcionou sem nenhuma reinstalação** — então
pode ter sido outra coisa. Se voltar a acontecer, o jeito de separar é o que preparamos e não
chegamos a rodar: com o app em DEBUG (cuidado com o BOM!), deixe o teclado parado por mais de
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
- 338 testes offline, todos verificados por mutação — cada correção foi remutada em memória para
  provar que o teste fica vermelho sem ela. Mantenha esse padrão: a suíte anterior tinha 180
  testes verdes e **não pegou nenhum** dos defeitos que o hardware achou.
- Três auditorias adversariais (contrato, concorrência, modos de falha) mais duas rodadas de
  correção estão no histórico. 28 defeitos corrigidos antes do primeiro boot.
- O que o hardware achou e nenhum teste acharia: CUDA caindo para CPU em silêncio, as
  reticências, e o watchdog reinstalando por causa do mouse. Ligar a coisa continua valendo mais
  que qualquer auditoria.
