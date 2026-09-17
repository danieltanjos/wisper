# tests — suíte offline

**Pode rodar com o PC em uso, inclusive com jogo aberto.** Essa é a regra da pasta, e ela não é
negociável: uma sessão anterior deste projeto sequestrou o teclado do usuário no meio de uma
partida, e nenhum teste daqui pode chegar perto de repetir isso.

```powershell
.\tests\run_all.ps1                      # tudo
.\tests\run_all.ps1 -Pattern test_dsp.py # só um arquivo
.\tests\run_all.ps1 -Quiet               # resumo
```

Sem PowerShell:

```powershell
python -m unittest discover -s tests -t tests -v
```

## O que a suíte garantidamente NÃO faz

| proibido | como isso é garantido |
|---|---|
| abrir microfone / stream de áudio | `sounddevice` e `soundfile` viram stubs em `sys.modules` **antes** de qualquer `import wispr`, então `Pa_Initialize` nunca roda e nenhum endpoint é aberto |
| carregar modelo / alocar VRAM | `faster_whisper`, `ctranslate2` e `torch` também são stubs; só o Python puro de `wispr/stt.py` executa |
| instalar hook de teclado | `SetWindowsHookExW` é interceptado no nível do `ctypes` e vira um objeto que estoura se alguém chamar |
| injetar tecla ou roubar um chord | idem para `SendInput`, `keybd_event`, `mouse_event`, `BlockInput`, `SetCursorPos` e `RegisterHotKey` |
| ler ou escrever o clipboard | idem para `OpenClipboard`, `SetClipboardData`, `EmptyClipboard`, `GetClipboardData`, `EnumClipboardFormats` |
| abrir janela, tray ou caixa de mensagem | `tkinter`, `pystray` e `PIL` são stubs; `CreateWindowExW` e `MessageBoxW` estão bloqueados |
| apitar no fone | `winsound` é stub: `PlaySound`/`Beep` abrem endpoint de saída e o ping do app sairia alto no meio da partida |
| baixar qualquer coisa | nenhum teste chama `Engine.transcribe()`, que é o único caminho de rede (o `wispr/stt.py` usa `urllib.request`, não `requests` — o stub de `requests` é cinto e suspensórios) |
| escrever no projeto | tudo que grava arquivo usa `tempfile.mkdtemp()`; até o `ensure_dirs()` é redirecionado |

Tudo isso mora em **`_safety.py`**, que todo `test_*.py` importa na primeira linha útil.
Ele é idempotente e não depende de ordem entre os arquivos.

### O que roda de verdade, e por quê

`comtypes` **não** é stub, de propósito: ele só faz `CoInitializeEx` e declarar structs — não
encosta em hardware —, o próprio `wispr/__init__.py` o importa no import do pacote, e as interfaces
COM de `audio.py` são declaradas com `ctypes.POINTER(IUnknown)`, que exige um tipo ctypes real. Com
stub, `wispr/audio.py` nem importaria e os testes de DSP e de ring sumiriam num skip. Nenhum objeto
COM chega a ser criado: `CoCreateInstance` está na lista de bloqueio e `Mic.__init__` nunca é
chamado.

`numpy`, `scipy` e `soxr` também rodam de verdade — é CPU pura, alguns segundos, zero VRAM.

E importar `wispr.stt` executa `_add_cuda_dll_dirs()` no nível do módulo: ele importa o namespace
package `nvidia` e chama `os.add_dll_directory()` nos `bin` das wheels. Isso só acrescenta pastas ao
caminho de busca de DLL **deste** processo — nenhuma DLL é carregada, nenhum contexto CUDA é criado
e nenhum byte de VRAM é alocado. O `cublas64_12.dll` só seria tocado por um `transcribe()`, e
nenhum teste chega perto disso.

## Os arquivos

| arquivo | cobre |
|---|---|
| `test_config.py` | defaults, round-trip de `save`/`load`, json corrompido caindo nos defaults, e a garantia de que `save` grava **só o diff** |
| `test_postprocess.py` | os `fixups` (branch / docker compose / middleware), colapso de espaço em branco, e a lista negra de alucinação disparando só num segmento curto e sozinho |
| `test_sanitize.py` | controle removido, CRLF normalizado, `\n` final removido (o stop key é **Enter**: um `\n` sobrando dispara a tecla de novo), acentos e caracteres fora do BMP preservados |
| `test_dsp.py` | `to_whisper()` em sinais sintéticos: 48k→16k, silêncio puro **não** amplificado, pico em -3 dBFS, passa-alta de 80 Hz matando 40 Hz e preservando 1 kHz |
| `test_ring.py` | a aritmética do ring buffer do microfone: pré-roll, virada do buffer, detecção de `lapped` e a faixa de guarda de um bloco |
| `test_regressions.py` | as cinco falhas achadas na auditoria, que **nenhum** dos arquivos acima pegava (detalhe abaixo) |

Em `test_ring.py` cada família de casos roda duas vezes, contra a réplica `FakeRing` e contra o
`Mic` de verdade criado com `object.__new__`. São duas famílias porque o `take()` reserva uma faixa
de guarda de um bloco (`room = size - block`) que só liga quando o ring tem mais de quatro blocos:
`_RingCases` usa um ring pequeno com a guarda **desligada** (é onde a aritmética de pré-roll e de
virada fica legível) e `_GuardBandCases` usa os números de produção, onde ela está **ligada** — e
sempre está, porque o `Mic.__init__` limita `block` a `ring.size // 8`. O `block` é plantado à mão
nos dois lados: sem isso o `Mic` real cairia no `BLOCK` do módulo e a guarda ligaria de um lado só,
com os dois "passando".

## `test_regressions.py` — as cinco da auditoria

Quatro das cinco falhavam **em silêncio** sob `pythonw.exe`, que é o que as tornava caras:

| regressão | sintoma que o teste impede de voltar |
|---|---|
| `Mic._emit` | vários eventos carregam o *friendly name* do endpoint em `name=`, e o parâmetro do próprio `_emit` se chama `name`: com parâmetro comum o Python recusa a chamada inteira, em **toda** abertura de microfone e no detector de silêncio digital. Os nomes de evento são varridos do fonte de `wispr/audio.py`, então um evento novo entra no teste sozinho |
| tamanho do ring | `ring_sec` menor que `max_record_sec` corta o fim do ditado longo sem erro nenhum; e `_num()` **prende** no limite em vez de voltar ao default, senão `ring_sec: 300` viraria 180 caladamente |
| `Delivery` | `inject.deliver()` devolve uma subclasse de `str`, nunca um `dict`: um `isinstance(res, dict)` no `app.py` desligava as três redes de segurança pós-entrega de uma vez. O dublê `FakeDelivery` do teste é escrito à mão de propósito — o `app.py` tem que ler por **atributo** — e há um caso a mais com o `Delivery` de verdade, para o dublê não passar sozinho se a classe mudar |
| crash no `on_stop` | o estado vira `transcribing` **antes** do trabalho; exceção depois disso prendia a máquina ali para sempre. O teste prova a recuperação rodando um ditado inteiro depois do acidente, até o job entrar na fila |
| `Esc` na transcrição | o cancelamento é gateado por `cancellable` (que sobrevive ao Enter), nunca por `recording`. Tem controle negativo: sem o `Esc`, o mesmo `_process` entrega o texto |

O `App` desses testes é real, com bandeja, pílula, microfone, motor e `inject` dublados, `sounds=False`
e `ensure_dirs` neutralizado. O `Mic` nasce de `object.__new__`, como em `test_ring.py`. O hook proc não
dá para chamar à mão (o `lParam` é um ponteiro do SO), então a garantia de que o `Esc` casa com
`cancellable` é lida do fonte de `_hookproc`.

## `fixtures/` não é desta suíte

A pasta `tests/fixtures/` tem wavs e `refs.txt` (transcrições de referência) para medir **WER**.
Medir WER exige carregar o modelo e a GPU, o que é exatamente o que esta suíte não pode fazer.
Então o aferidor de WER **não pode se chamar `test_*.py`**: `run_all.ps1` descobre por esse padrão
e passaria a carregar CUDA junto com os testes. Dê a ele outro nome (`bench_wer.py`, por exemplo) e
rode à mão, com o PC livre.

Vale para qualquer arquivo novo: se ele entrar aqui como `test_*.py`, entra nesta corrida e
herda a regra da pasta.

Três armadilhas já plantadas em `fixtures/`, para quem for escrever esse aferidor não descobrir
sozinho:

- `refs.txt` lista **quatro** referências (`ptbr_short`, `ptbr_dev1`, `ptbr_plain`, `ptbr_dev2`) e a
  pasta tem **três** wavs. Falta `ptbr_plain.wav`.
- o wav que corresponde a `ptbr_dev1` se chama **`ptbr_dev1_noisy.wav`**: casar chave com nome de
  arquivo por igualdade exata não funciona.
- `refs.txt` começa com **BOM** de UTF-8. Lido com `encoding="utf-8"`, a primeira chave vira
  `"﻿ptbr_short"` e some do casamento sem erro nenhum. Use `encoding="utf-8-sig"`.

## Por que alguns testes pulam

Os módulos de `wispr/` estão sendo escritos em paralelo, e um teste que ainda não tem o que testar
**pula com a razão escrita**, em vez de ficar vermelho. As mensagens são específicas de propósito —
`wispr.stt indisponivel (ImportError: ...)` diz mais do que um `E`. Também pulam se `numpy`,
`soxr` ou `scipy` não estiverem instalados (rode `scripts\setup.ps1`; a suíte nunca instala nada
por conta própria).

Dois pulos merecem atenção porque apontam para uma costura do código, e não para um módulo
ausente:

- **`test_sanitize.py` inteiro pula** se nenhum módulo expuser uma função pura de saneamento.
  Hoje ele encontra `wispr.inject.sanitize`. Essa função **não** está em `docs/CONTRACT.md` (que só
  lista `deliver/type_unicode/paste_text/...`, e todas essas injetam de verdade), então o teste a
  procura por nome — `sanitize`, `sanitize_text`, `clean_text` e afins — em `wispr.inject`,
  `wispr.stt`, `wispr.text` e `wispr.util`. Se ela for renomeada, o teste some em silêncio.
- **`RealMicRingTest` / `RealMicGuardBandTest` pulam** se `Mic._cb/mark/take/_slice` deixarem de
  existir. Quando eles pulam, quem está sendo testado é só a réplica `FakeRing` — a especificação,
  não o código de produção. O harness planta à mão os atributos que esses quatro métodos leem hoje
  (`ring`, `written`, `sr`, `ring_sec`, `block`, `preroll_sec`, `silence_rms`, `xruns`,
  `last_audio`, `last_rms`, `device`, `name`, `_cb_err`, `_silent_t`, `on_event`); se algum for
  renomeado, o teste quebra com `AttributeError` apontando o nome — que é exatamente o aviso que se
  quer. Se algum deixar de ser lido, ninguém fica vermelho: o plantio vira peso morto.

Os testes de alucinação dependem de outra extensão do contrato: `postprocess` aceita
`single_short_segment=True` por keyword, e é **só** com essa flag que a lista negra pode apagar
alguma coisa. A chamada de duas posições do `CONTRACT.md` nunca censura nada, e existe um teste
exatamente para isso. Se a flag sumir, os testes caem para a chamada simples sozinhos.
