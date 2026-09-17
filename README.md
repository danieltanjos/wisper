# wisper

Ditado por voz que vive na bandeja do Windows 11. Você aperta `Win+A`, fala, aperta `Enter`, e o
texto aparece digitado dentro da janela que estava em foco — Notion, VS Code, WhatsApp Web, o que
for. Não é push-to-talk: você não segura nada, são duas teclas, uma para abrir e outra para fechar.
A transcrição roda **local**, na sua RTX 4060 Ti, sem mandar áudio para lugar nenhum.

| tecla | o que faz |
|---|---|
| **Win+A** | começa a gravar |
| **Enter** | finaliza, transcreve e digita na janela em foco |
| **Esc** | cancela e joga fora o áudio |

---

## Por que isso NÃO usa a sua assinatura do ChatGPT Plus

Você pediu para usar os tokens da conta Plus em vez de uma API key, e a resposta honesta é que
**a conta Plus não dá acesso a transcrição de áudio**: o subsistema realtime do `codex` responde
literalmente `realtime conversation requires API key auth` nas três versões do protocolo, o caminho
não-realtime aceita o áudio no schema e devolve `SEM_AUDIO`, o catálogo de modelos da conta só tem
modelos de texto e imagem, e o `access_token` OAuth não carrega nenhum escopo de áudio — isso foi
testado até o fim, não é suposição (ver `docs/ARCHITECTURE.md`, seção 1).

O único caminho que funcionaria seria raspar `chatgpt.com/backend-api/transcribe` com o token de
sessão do navegador, e isso viola os Termos de Uso da OpenAI, então está fora de questão — por isso
o STT é local, o que no fim das contas é melhor para você: custo zero, sem quota, sem internet, e
mais rápido (0,4–0,6 s por fala) do que qualquer ida e volta na nuvem.

> Sobra um detalhe prático: como caminho de **texto** o `codex` funciona, mas custa 2,2–3,0 s por
> turno e ~12–15k tokens de input por chamada trivial — 4 chamadas de teste comeram 11% da janela
> de quota de 5 h. Por isso a conta Plus não entra no caminho quente do ditado, no máximo num
> comando manual de "polir transcrição".

---

## Instalação

Requisitos: Windows 11, RTX 4060 Ti (ou qualquer GPU NVIDIA com ~1,5 GB livres) e
**CPython 3.11 do python.org** — não o da Microsoft Store.

> O Python da Store tem identidade de pacote e uma entrada própria no ConsentStore do microfone:
> em "Prompt" a abertura do stream **trava para sempre, sem exceção**, esperando uma UI de
> consentimento que nunca aparece; depois vira "Deny" e falha com `-9999` em todo device. Ele ainda
> redireciona silenciosamente escritas em `%LOCALAPPDATA%` e `%APPDATA%` para o sandbox do pacote.
> Se `where python` apontar para `WindowsApps`, é o errado.

Três passos, nessa ordem, no PowerShell dentro da pasta do projeto:

```powershell
# 1. valida o interpretador, cria o .venv, instala tudo e prepara as pastas
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# 2. sobe o app na bandeja (é isso que você roda no dia a dia)
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1

# 3. faz ele subir sozinho no logon
powershell -ExecutionPolicy Bypass -File .\scripts\autostart_install.ps1
```

O `setup.ps1` é idempotente — pode rodar de novo à vontade. Ele **recusa** o Python da Store de
propósito. Os pesos do Whisper (~1,6 GB) baixam sozinhos no primeiro ditado; se você preferir
resolver isso agora, com barra de progresso e num momento em que não vai atrapalhar, use
`.\scripts\setup.ps1 -DownloadModel`.

O `run.ps1` lança `pythonw.exe main.pyw` destacado: não abre console e sobrevive ao fechamento do
shell. Como não existe stdout sob `pythonw.exe`, **tudo** vai para `logs\wisper.log`. Quando algo
estiver estranho, rode `.\scripts\run.ps1 -Console`: aí é `python.exe` em primeiro plano, com os
erros à vista. Para encerrar: "Sair" no menu da bandeja, ou `.\scripts\stop.ps1`.

O `autostart_install.ps1`, por padrão, grava o valor `wisper` em
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — gravável sem elevação. Se você quiser que o
**Win+A funcione mesmo com uma janela de administrador em foco**, rode-o com `-Task`: aí ele cria
uma Tarefa Agendada `ONLOGON` com privilégio mais alto, e o Windows pede UAC **uma vez** (sem
elevação, `schtasks /Create` falha com "Acesso negado" mesmo sem `/RL HIGHEST`). As duas formas
juntas lançariam dois processos no logon, e o segundo morreria calado no mutex — por isso o `-Task`
remove a entrada do registro, a menos que você passe `-KeepRunKey`.

Para desinstalar o autostart: `.\scripts\autostart_uninstall.ps1` (ou `-Task` para remover a
tarefa agendada).

---

## Uso no dia a dia

1. Deixe o cursor onde você quer o texto (campo de busca, editor, caixa de mensagem).
2. **Win+A** — a pílula aparece na parte de baixo da tela com as barrinhas mexendo.
3. Fale.
4. **Enter** — a pílula muda para "transcrevendo" e em menos de um segundo o texto é digitado.
5. Se falou besteira, **Esc** antes do Enter: o áudio é descartado e nada é injetado.

Texto curto (até ~120 caracteres) é **digitado** caractere a caractere; acima disso o app **cola**
via clipboard e restaura o que você tinha antes. Isso é medido: digitar tem latência visível de
~1,7 ms/char (5.000 caracteres levariam 10 s), colar é plano em ~80 ms em qualquer tamanho.

O ícone da bandeja mostra o estado (parado / gravando / trabalhando / erro). No menu dele você tem
**"Colar última transcrição"** (recoloca o último texto sem precisar falar de novo), **"Histórico"**,
**"Motor"** para escolher entre *Local (GPU)* e *Groq (nuvem)*, **"Diagnóstico"** (microfone,
STT, elevação e hook — é o primeiro lugar para olhar quando algo não funciona), **"Pausar ditado"**,
**"Abrir configuração"**, **"Abrir logs"** e **"Sair"**.

A troca de **"Motor"** é gravada no `config.json` na hora, mas o modelo em memória não é trocado a
quente: o balão diz *"será usado no próximo início"* e é isso mesmo que acontece. Para valer agora,
`.\scripts\stop.ps1` e `.\scripts\run.ps1`.

---

## `config.json` — as chaves que importam

O arquivo fica na raiz do projeto e só guarda o que **difere** do padrão, então ele começa como
`{}` e cresce conforme você mexe. Abra pelo menu da bandeja ("Abrir configuração"), edite, salve e
**reinicie o app** (`.\scripts\stop.ps1` e depois `.\scripts\run.ps1`) — a configuração é lida no
boot.

Se o JSON estiver quebrado (vírgula sobrando, aspas faltando), o app **volta inteiro aos padrões em
vez de morrer** — e faz isso **em silêncio**, sem nada no log: a configuração é lida antes de o log
existir, então não há onde escrever o aviso. O sintoma, portanto, é "mexi no arquivo e não mudou
nada". Se isso acontecer, cole o conteúdo do `config.json` em qualquer validador de JSON antes de
procurar bug em outro lugar.

### `hotkey`
`"win+a"` (padrão) ou `"ctrl+alt+a"`.
O `Win+A` só funciona porque o app instala um hook de teclado de baixo nível (`WH_KEYBOARD_LL`) e
**engole** o evento antes do shell — `RegisterHotKey(MOD_WIN,'A')` é recusado com erro 1409, o
Windows 11 é dono de todos os `Win+letra`. O hook é o único caminho que existe para essa tecla.

`"ctrl+alt+a"` é uma alternativa se o `Win+A` brigar com algum outro programa, mas ela **não** muda
a mecânica: o app continua usando o mesmo hook de baixo nível para os dois chords, porque é ele que
também escuta o `Enter` e o `Esc` durante a gravação. O que muda é só que o `Ctrl+Alt+A` não
disputa a tecla com o shell — e, por isso, não precisa da tecla-máscara que desarma o menu Iniciar.

### `initial_prompt` — **o maior ganho de acurácia disponível**
É um glossário curto que o Whisper lê antes de transcrever. Colocar aqui o seu jargão —
nomes de projeto, de clientes, de bibliotecas, siglas internas — derrubou o WER de **13,4% para
8,5%** nos testes desta máquina. Duas regras que não são óbvias:

- **Escreva SEM ACENTOS.** A versão acentuada do mesmo glossário ficou em 9,8%, a sem acentos em
  8,5%. Não pergunte, só faça. (O texto transcrito sai acentuado normalmente.)
- **Mantenha curto.** O `faster-whisper` trunca o prompt em 224 tokens; um prompt de 368 tokens
  ficou **pior do que não ter prompt nenhum**. Umas 3 linhas é o ponto certo.

Não use `hotwords`: ele é pior que o prompt e ainda **cancela o benefício dele** quando os dois
são usados juntos.

Para o punhado de palavras que o prompt comprovadamente não conserta (`branch` volta como `brand`
em todo modelo e toda variante de prompt), existe a lista `fixups`, que é uma busca-e-troca literal
aplicada no texto final:

```json
"fixups": [[" brand ", " branch "], ["docker campus up", "docker compose up"]]
```

### `engine` — `"local"` ou `"groq"`
`"local"` (padrão) roda o `faster-whisper-large-v3-turbo` na sua GPU: 1.024 MB de VRAM,
0,40–0,62 s por fala de 6–15 s, offline e de graça.
`"groq"` manda o áudio para a API da Groq e **nunca carrega modelo local** — útil quando a VRAM
está toda ocupada por um jogo. O tier gratuito da Groq (20 req/min, 28.800 s de áudio por dia)
cobre com folga 90 min de ditado por dia. Ponha a chave em `groq_api_key` ou na variável de
ambiente `GROQ_API_KEY`.

### `inject_mode` — `"auto"`, `"type"` ou `"paste"`
`"auto"` (padrão) digita até `inject_threshold` (120) caracteres e cola acima disso — e também cola,
independentemente do tamanho, quando o texto tem quebra de linha, porque no caminho digitado o
`\n` sai como caractere Unicode e vários controles de edição simplesmente o descartam.
`"type"` força sempre digitar: nunca toca no seu clipboard, mas é lento em texto longo.
`"paste"` força sempre colar: instantâneo, porém passa pelo clipboard — se você tiver uma **imagem**
copiada, o restore só devolve texto e a imagem se perde (o app avisa no log quando isso acontece).
Relacionadas: `restore_clipboard` (padrão `true`) e `clipboard_restore_delay` (padrão 0,25 s —
com 0 ms o app de destino cola o conteúdo **antigo**; 20 ms foi o mínimo que funcionou aqui).

### outras que você pode querer mexer
| chave | padrão | para quê |
|---|---|---|
| `preload_model` | `true` | carrega o modelo no boot em vez de no primeiro Win+A |
| `compute_type` | `"int8_float16"` | precisão na GPU; `"int8"` gasta menos VRAM |
| `device` | `"cuda"` | cai sozinho para `"cpu"` se a CUDA falhar |
| `language` | `"pt"` | trave no idioma, é mais rápido e mais preciso |
| `max_record_sec` | `175` | corta a gravação sozinho se você esquecer o Enter |
| `ring_sec` | `180.0` | o teto **real** de um ditado; acima disso o começo é sobrescrito |
| `overlay` | `true` | a pílula flutuante |
| `sounds` | `true` | ping de início/fim |
| `keep_recordings` | `false` | salva o wav de cada ditado em `logs/recordings` para depurar |
| `log_level` | `"INFO"` | ponha `"DEBUG"` antes de reportar um problema |

---

## Solução de problemas

### "Apertei Win+A e abriu a Central de Ações"
Duas causas, nessa ordem de probabilidade:

1. **Já tem outra instância rodando.** O app usa um mutex (`Local\WisprCloneDaniel`); a segunda
   cópia detecta que perdeu e sai calada, então você fica olhando para um processo que não é o que
   está com o hook. Feche tudo com `.\scripts\stop.ps1` e suba uma só com `.\scripts\run.ps1`.
2. **A janela em foco é elevada.** Com o Gerenciador de Tarefas como admin (ou qualquer app
   rodando elevado) em primeiro plano, o UIPI faz o hook receber **zero eventos** e o `Win+A` vaza
   para o shell. Não tem contorno por software: a correção é rodar o wisper elevado
   (`.\scripts\autostart_install.ps1 -Task`).

Se não for nenhum dos dois, olhe `logs/wisper.log`: o app tem um watchdog que reinstala o hook
quando o Windows o despeja (acontece se algum callback demorar demais) e ele registra cada
reinstalação.

### "Falei, apertei Enter, e não apareceu nada — mas o app diz que transcreveu"
A janela em foco é **elevada**. Dentro dela, tanto o `SendInput` quanto o `Ctrl+V` são descartados
silenciosamente pelo Windows — `GetLastError()` chega a retornar 0, ou seja, o sistema mente
dizendo que deu certo. O app percebe isso e deixa o texto **no seu clipboard** (o balão diz
"Copiei para a área de transferência: use Ctrl+V"): clique numa janela normal e dê `Ctrl+V`, ou use
**"Colar última transcrição"** no menu da bandeja. Correção permanente: rodar o wisper elevado
(`.\scripts\autostart_install.ps1 -Task`).

### "Microfone mudo" na pílula / no balão
Este headset sem fio às vezes entrega **todas as amostras exatamente 0.0** enquanto o Windows jura
que está tudo bem: endpoint `Active`, não mudo, volume 0,85, stream ativo, callbacks no horário,
zero falhas — e o próprio medidor do Windows lê 0,0. **Nenhuma API do sistema reporta erro**; só um
teste de RMS no áudio gravado detecta, e é exatamente isso que essa mensagem é.

Para confirmar: abra *Configurações › Sistema › Som › Microfone* e fale — se a barrinha de teste do
próprio Windows não mexer, não é o wisper. A correção que funciona é desligar e religar o headset
(ou tirar e recolocar o dongle); o app reabre o stream sozinho quando o endpoint volta. Se a
barrinha do Windows mexe e mesmo assim dá "mudo", **diminua** `silence_rms` (o padrão é `3e-5`;
tente `5e-6`): esse número é o piso de RMS abaixo do qual o áudio é considerado silêncio digital,
então quem está sendo recusado por falar baixo precisa de um piso **menor**, nunca maior. Não
adianta pôr `0`: o app piso-limita em `1e-12` para o gate nunca sumir (silêncio digital é RMS
exatamente `0.0`, então qualquer limiar positivo o pega), e com um piso desses qualquer chiado
passa e o Whisper alucina legenda em cima de ruído.

### "A primeira ditada do dia demora uns 10 segundos"
É o modelo carregando na GPU. Deixe `"preload_model": true` (é o padrão) para ele carregar no boot
do app em vez de no seu primeiro `Win+A`. Se for a **primeira vez** depois de instalar, soma-se o
download dos pesos (~1,6 GB), que só acontece uma vez — rode
`.\scripts\setup.ps1 -DownloadModel` num momento tranquilo e o primeiro ditado já sai rápido.

### "Deu erro de VRAM / o jogo engasgou quando eu ditei"
A 4060 Ti tem 8 GB, mas com o desktop em uso (Wallpaper Engine, navegador, WhatsApp) sobraram
medidos **1.746 MiB** — com um jogo aberto, bem menos. O `int8_float16` já foi escolhido por isso
(1.024 MB), mas se ainda estourar, você tem três saídas, da menos para a mais drástica:

```jsonc
{ "compute_type": "int8" }     // menos VRAM, mesma qualidade prática
{ "engine": "groq" }           // zero VRAM, usa a nuvem, tier gratuito basta
{ "device": "cpu" }            // zero VRAM, funciona, mas fica lento
```

### "Os acentos vêm errados" / "ele escreve 'brand' em vez de 'branch'"
Duas ferramentas diferentes, nessa ordem:

1. **`initial_prompt`**: acrescente o termo, **sem acento**, na lista de jargão. É o que resolve
   90% dos casos, e é de longe o maior ganho disponível (ver a seção do config acima).
2. **`fixups`**: para o que o prompt não conserta de jeito nenhum. É troca literal, então inclua os
   espaços em volta para não estragar palavras maiores (`" brand "` e não `"brand"`).

Se o texto sai com acentuação **quebrada** (caracteres estranhos, e não palavras erradas), aí não é
o modelo: é injeção. O app digita por `KEYEVENTF_UNICODE`, que é independente de layout e foi
verificado nos três layouts desta máquina (en-US, ABNT2, pt-BR US-Intl). Reporte com
`log_level: "DEBUG"`.

---

## Como isso funciona

Resumo de uma tela; o detalhe todo, com os números medidos, está em **`docs/ARCHITECTURE.md`**.

O app roda em quatro threads: a principal segura o ícone da bandeja, uma thread dedicada segura o
hook de teclado `WH_KEYBOARD_LL`, outra desenha a pílula em Tk, e a *worker* faz o trabalho pesado.
O microfone fica com o stream WASAPI **sempre aberto** gravando num ring buffer — não por latência,
mas por **pré-roll**: quando você aperta `Win+A`, o app rebobina 0,35 s para trás, então a primeira
sílaba nunca é cortada. Esse ring guarda `ring_sec` (180 s) de áudio e é o teto real de um ditado
— o que passa disso é sobrescrito antes do `Enter` e some sem erro nenhum, e é por isso que o
`max_record_sec` (175 s) corta a gravação um pouco antes. No `Enter` o áudio é reamostrado de
48 kHz para 16 kHz com `soxr`, passa por
um passa-alta de 80 Hz e é normalizado para -3 dBFS (o Whisper não é invariante a ganho), e vai para
o `faster-whisper` na GPU. O texto sai pelo `postprocess` (fixups, espaços, filtro de alucinação) e
é entregue por `SendInput` ou por clipboard, conforme o tamanho.

Os arquivos que interessam:

```
config.json          suas preferências (só o que difere do padrão)
logs/wisper.log      1 MB, 5 backups — o primeiro lugar para olhar
logs/recordings/     wavs de depuração, só se keep_recordings = true
models/              cache do modelo (HF_HOME aponta para cá)
docs/ARCHITECTURE.md tudo que foi medido, e por que cada decisão é o que é
docs/CONTRACT.md     a API pública de cada módulo
tests/               suíte offline: roda sem microfone, sem GPU e sem janela
```

---

## Limitações — o que este programa não faz

- **Janelas elevadas são um buraco de verdade.** Se o app não estiver rodando elevado, uma janela
  de administrador em foco quebra as duas pontas ao mesmo tempo: o hook não recebe o `Win+A` (que
  vaza para a Central de Ações) e o `SendInput` é descartado sem erro. Não é bug, é o UIPI do
  Windows fazendo o trabalho dele. A única solução é o wisper rodar elevado também — e aí ele passa
  a ser um processo elevado com um hook global de teclado, o que é um poder que você deve conceder
  conscientemente. Por padrão, o instalador **não** faz isso.
- **Não usa a sua conta ChatGPT Plus** — ver a segunda seção deste arquivo.
- **Uma instância por vez.** Não dá para rodar duas cópias (nem faria sentido: uma só pode segurar
  o hook).
- **Sem streaming.** O texto aparece de uma vez no fim, não enquanto você fala. É a mesma escolha
  do WisprFlow original.
- **Português por padrão.** Funciona em outros idiomas mudando `language`, mas o glossário, os
  `fixups` e o filtro de alucinação foram calibrados para pt-BR.
- **Roda do fonte.** Não tem instalador `.exe` ainda; precisa do Python 3.11 do python.org
  instalado na máquina.
- **O clipboard é compartilhado.** No modo `paste`, se você tiver uma imagem copiada, o restore
  devolve só o texto e a imagem se perde. Use `"inject_mode": "type"` se isso for inaceitável.
