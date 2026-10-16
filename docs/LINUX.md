# wisper no Linux — KDE Plasma 6, Wayland, PipeWire

Port feito em 2026-10-15 no notebook (Fedora 44 KDE, kernel 6.19, Plasma/KWin 6.6.4,
i5-13420H, Intel UHD, **sem GPU NVIDIA**, 16 GB). Mesmo app, mesmas teclas: `Meta+A` grava,
`Enter` cola o texto na janela em foco, `Esc` cancela. O que está aqui foi **medido** nesta
máquina, como o resto da documentação; o que não pôde ser medido está marcado como tal.

```bash
./scripts/setup.sh --download-model   # venv, executável próprio, .desktop, modelo
./scripts/run.sh --console            # primeiro plano, Ctrl+C sai
./scripts/run.sh                      # segundo plano; log em logs/wisper.log
./scripts/autostart_install.sh        # sobe no login do Plasma
./tests/run_all.sh                    # suíte offline (pytest)
```

Nada disso usa `sudo`. O único pacote de sistema que faltava aqui é o `python3-tkinter` (só a
pílula): `sudo dnf install python3-tkinter` — ou `sudo ./scripts/setup-linux-root.sh`.

---

## 1. O que muda e onde mora

| peça | Windows | Linux | arquivo |
|---|---|---|---|
| atalho global | `WH_KEYBOARD_LL` | kglobalaccel do KDE por D-Bus | `wispr/linux/hotkey.py`, `wispr/linux/kde.py` |
| Enter/Esc só durante o ditado | o hook decide tecla a tecla | Return/Escape **registrados e soltos** no kglobalaccel conforme `recording`/`cancellable` | idem |
| proteção contra crash | — | guardião em processo filho | `wispr/linux/keyguard.py` |
| injeção | `SendInput` unicode / Ctrl+V | clipboard (`wl-copy`) + Shift+Insert pelo `org_kde_kwin_fake_input` | `wispr/linux/inject.py`, `wispr/linux/wayland.py`, `wispr/linux/clipboard.py` |
| microfone | WASAPI + COM | ALSA → device `pipewire` | `wispr/audio.py` (`pick_linux_device`) |
| STT | turbo na 4060 Ti | `Systran/faster-whisper-small`, CPU int8 | `wispr/config.py` (`LINUX_DEFAULTS`) |
| bandeja | pystray win32 | pystray AppIndicator → StatusNotifierItem | `wispr/tray.py` (mesmo arquivo) |
| pílula | Tk + ex-styles Win32 | Tk no XWayland, override-redirect | `wispr/overlay.py` (mesmo arquivo) |
| instância única | mutex `Local\WisprCloneDaniel` | `flock` em `$XDG_RUNTIME_DIR/wisper.lock` | `wispr/app.py` |
| scripts | `scripts/*.ps1` | `scripts/*.sh` | |

`wispr.hotkey` e `wispr.inject` continuam sendo os nomes importados por todo mundo. No Linux o
`wispr/__init__.py` instala um finder que carrega `wispr/linux/hotkey.py` e `wispr/linux/inject.py`
**com esses nomes** — o `app.py`, a bandeja, a suíte e o logger (`wisper.inject`) não sabem a
diferença. Os arquivos Windows `wispr/hotkey.py` e `wispr/inject.py` não foram tocados. `audio`,
`stt`, `overlay`, `tray` e `app` são o mesmo arquivo nas duas plataformas, com ramos
`sys.platform`; os blocos Win32 do topo de `app.py` e `overlay.py` recebem um substituto que aceita
`argtypes`/`restype` e estoura se for chamado (`wispr/win32stub.py`).

`Delivery`, `sanitize()` e `utf16_units()` são puros, mas moram no `wispr/inject.py`, que não importa
fora do Windows. `wispr/delivery.py` é uma **cópia fiel**, e `tests/test_linux.py::DeliveryParityTest`
compara o fonte das duas e fica vermelho na primeira diferença. Mudou lá, muda aqui.

## 2. Atalho global: kglobalaccel

No Wayland nenhum app comum vê tecla de outra janela. Quem vê é o compositor, e no Plasma o dono dos
atalhos globais é o kglobalaccel (`org.kde.kglobalaccel`). O wisper registra o componente `wisper`
com três ações e escuta o sinal `globalShortcutPressed` em `/component/wisper`. A tecla é consumida
pelo KWin e não chega à janela em foco — o mesmo efeito do `return 1` do hook do Windows.

- `dictate` (`Meta+A`): registrado no boot e mantido a sessão inteira. **Sem** `NoAutoloading`: se
  você trocar a tecla em *Configurações do Sistema → Atalhos → wisper*, vale a sua, e o
  `hotkey` do `config.json` vira só o padrão do primeiro registro.
- `stop` (`Return` **e** o `Enter` do numérico) e `cancel` (`Escape`): **só existem enquanto
  `recording` / `cancellable` estão ligados**. São as mesmas duas flags do Windows, com os mesmos
  donos (ver `CONTRACT.md`): o Enter só é do wisper depois que o app confirmou a gravação; o Esc vale
  até o texto ser colado. Escrever na flag acorda a thread `hk-arm`, que registra ou solta a tecla
  no kglobalaccel fora do lock do app.

Medido com F24 (que não existe no teclado, para não roubar tecla de ninguém):
tecla sem modificador é aceita; `invokeShortcut` → `globalShortcutPressed` chega em < 1 ms;
`Meta+A`, `Return` e `Escape` estavam livres (`isGlobalShortcutAvailable` → true).

**O perigo, medido:** o kglobalaccel **mantém o atalho ativo depois que o processo dono morre**
(`isActive` true, `getGlobalShortcutsByKey` ainda acha). Para `Meta+A` isso é só uma tecla morta;
para `Return` e `Escape` seria o Enter e o Esc engolidos no sistema inteiro. Três cintos:

1. desligamento normal (`Sair`, SIGTERM): `HotkeyEngine.stop()` solta Return/Escape e desativa
   o Meta+A;
2. crash, OOM, `kill -9`: o **guardião** — `python -m wispr.linux.keyguard`, processo filho em
   sessão própria com um pipe no stdin — vê o EOF quando o pai morre e solta tudo. **Medido:** com
   F24/F23 armados, `kill -9` no processo do engine; 1,5 s depois os dois estavam soltos e o
   componente inativo. Se o lock de instância única estiver preso no EOF, um wisper novo já subiu
   e o guardião não mexe em nada;
3. até o guardião morreu junto (`kill` no grupo/cgroup inteiro): o próximo `start()` solta
   Return/Escape antes de qualquer coisa, e `./scripts/release-keys.sh` faz o mesmo na mão.

`./scripts/release-keys.sh --forget` apaga o grupo `[wisper]` das Configurações (desinstalação).

Alternativas descartadas: **evdev** (`/dev/input/event*`) exige o grupo `input`, e estar nele deixa
qualquer processo do usuário ler todas as teclas, inclusive senhas — o usuário não está no grupo e
não deve entrar; **portal GlobalShortcuts** pede confirmação em diálogo a cada `BindShortcuts`, um
diálogo por ditado para Return/Esc; **atalho personalizado chamando um CLI** resolve o Meta+A, mas
Enter e Esc teriam que ficar ligados para sempre.

## 3. Injeção: clipboard + Shift+Insert pelo fake_input do KWin

O que existe no Wayland para "digitar na janela dos outros", conferido com `wayland-info`:

- `wtype` precisa de `zwp_virtual_keyboard_manager_v1`. O KWin 6.6 **não anuncia** esse global
  para cliente comum. Descartado.
- `ydotool` / `/dev/uinput`: `crw------- root root`. Exige root (regra udev + grupo). Desnecessário.
- `org_kde_kwin_fake_input` (v6) é um global **restrito**: o KWin só o anuncia para o executável
  que um `.desktop` declara com `X-KDE-Wayland-Interfaces=org_kde_kwin_fake_input` (é o caminho do
  KDE Connect). **Medido:** um `.desktop` em `~/.local/share/applications` — do usuário, sem root —
  basta. O KWin casa o `Exec=` com o `/proc/<pid>/exe` do cliente; leva uns segundos para o cache
  do KService ver o arquivo novo (o `setup.sh` espera).
- `ext_data_control_manager_v1` está anunciado: `wl-copy`/`wl-paste` funcionam **sem foco**, sem
  janela invisível nossa.

O `/proc/<pid>/exe` de um script é o interpretador. Para não liberar o fake_input para **todo**
`python3.14` da máquina, o `setup.sh` copia o binário do Python para `.venv/bin/wisper` (cópia real;
com symlink o exe seria `/usr/bin/python3.14`) e o `.desktop` aponta para ele. O venv continua
valendo porque o `pyvenv.cfg` fica ao lado. **Rode sempre por `.venv/bin/wisper`** (`run.sh`, o
menu, o autostart). Atualizou o Python do sistema? Rode `setup.sh` de novo.

O cliente Wayland é Python puro sobre o socket (`wispr/linux/wayland.py`, ~250 linhas). Os opcodes
(`authenticate`=0, `keyboard_key`=10, `destroy`=11, `keyboard_keysym`=12) foram conferidos na tabela
`wl_message` do `libkwin.so.6.6.4`. Conexão curta por colagem: conecta, `bind`, `authenticate`,
teclas, `sync`, fecha — ~1 ms, e sobrevive a um KWin reiniciado.

**Por que não digitar** (o modo `type` do Windows): o fake_input fala keycode físico ou keysym. Keysym
de acento não tem tecla própria no ABNT2 (`ã` é tecla morta + `a`); o KWin não acha keycode e
**descarta em silêncio**. Digitar pt-BR por ali perderia letras sem erro nenhum. Todo texto vai pelo
clipboard, em qualquer tamanho — é o que o Wispr Flow faz (ARCHITECTURE.md seção 7). `inject_mode`
é ignorado no Linux.

**Por que Shift+Insert e não Ctrl+V** (`linux_paste_keys`): no Konsole Ctrl+V manda `^V`, não cola.
Shift+Insert cola em Qt, GTK, Firefox, Chromium/Electron e no Konsole. Alacritty/xterm/foot colam a
seleção *primary* no Shift+Insert, então o texto vai também para a primary
(`linux_paste_primary: true`). Keycodes evdev são físicos: Shift/Insert/Ctrl/V são os mesmos no US e
no ABNT2. Se algum app seu não colar, troque para `"ctrl+v"` ou `"ctrl+shift+v"`.

Depois de colar, o clipboard anterior volta (`restore_clipboard`), com `clipboard_restore_delay` de
**0,5 s** no Linux (0,25 no Windows): o app em foco lê a seleção de forma assíncrona, do processo do
`wl-copy`, depois de receber a tecla, e trocar a seleção antes disso cancela a transferência. **Não
medido** com apps reais — se algum colar o texto antigo, aumente. Imagem ou arquivos copiados no
Dolphin (`text/uri-list`) não sobrevivem a um restore só de texto; o app avisa, como o `CF_DIB` no
Windows. O Klipper vai registrar a transcrição no histórico dele.

Sem o fake_input liberado (setup não rodou, ou o app subiu por outro executável), o texto fica na
área de transferência e a pílula diz "use Ctrl+V" — o mesmo caminho do UIPI no Windows. O menu da
bandeja mostra *Diagnóstico → Injeção: colar automático / só clipboard*.

Não existe UIPI no Wayland: `target_is_elevated()` e `injection_blocked()` devolvem `False`.

## 4. Áudio: PipeWire pelo ALSA

O `sounddevice` do Linux usa o `libportaudio` do sistema (pacote `portaudio`, 19.7). O PortAudio do
Fedora fala ALSA, e o `pipewire-alsa` expõe o device **`pipewire`**, que segue a fonte padrão do
PipeWire — a que você escolhe no Plasma — e reamostra para os 48 kHz pedidos. É o escolhido por
padrão (`pick_linux_device`): nunca um `hw:` cru, que trava o hardware só para nós e ignora a escolha
do Plasma. `linux_mic_device` no `config.json` força um device por trecho do nome
(`python -c "import sounddevice; print(sounddevice.query_devices())"`). Troca de headset é do
PipeWire; o supervisor de stream continua valendo. `mic_on_demand` igual ao Windows.

**Não medido:** abertura de stream e captura real (a regra da máquina proíbe abrir o microfone sem
o usuário pedir na hora). Só a enumeração foi feita: device 8 `pipewire`, host API ALSA.

## 5. STT na CPU

Sem NVIDIA, o padrão do Linux é `device: cpu`, `cpu_compute_type: int8`, modelo
`Systran/faster-whisper-small` (baixa sozinho para `models/` no primeiro uso, ~480 MB). Nada de
CUDA: `_add_cuda_dll_dirs()` só age no Windows, e não há wheel `nvidia-*` no
`requirements-linux.txt`.

Lembrete do que já foi medido neste notebook no Windows (CLAUDE.md): o turbo na CPU custa 6,5–8 s
**fixos** por ditado; o `small` ~2,4 s, mas com WER 38% (79% com ruído). Se a qualidade não servir,
`"engine": "groq"` no `config.json` (a chave está no `.env`) dá ~0,5 s com o turbo na nuvem.
Com `engine: auto` (padrão) e `device: cpu`, o Groq entra **só** se o modelo local não carregar
(primeiro uso sem rede, disco cheio) — antes disso o app é 100% local.

## 6. Bandeja e pílula

Bandeja: pystray com backend AppIndicator (`libappindicator-gtk3`, typelib `AppIndicator3`), que fala
StatusNotifierItem — a bandeja do Plasma no Wayland. Precisa do `gi` (PyGObject) do Fedora, por isso
o venv é `--system-site-packages`. Roda o loop do GTK na thread principal, no lugar do bombeamento
Win32.

Pílula: o mesmo `wispr/overlay.py`, com Tk no XWayland. Uma janela `overrideredirect` do X11 não é
gerenciada pelo KWin e **nunca recebe foco** — não há NOACTIVATE a pedir. Mostrar/esconder é
`deiconify()`/`withdraw()` do Tk (no X11 isso não ativa nada). Centro horizontal: o monitor primário
do `xrandr --listmonitors` (a tela X é a união dos monitores; medido `3672x1920` com o notebook em
`2592x1458+1080+0`). Base: o `_NET_WORKAREA` do `xprop` (medido: `0, 0, 1920, 1034`, painel de 46 px),
que o KWin publica em px lógicos, vezes o `[Xwayland] Scale` do kwinrc (1,35 → 1396 em px X). Não há `-transparentcolor` no X11:
o wrapper do Tk (pai do `winfo_id()`; o `wm frame` devolve o próprio `winfo_id()` sem reparent) é
recortado no formato da cápsula pela extensão SHAPE, por ctypes na libXext. Sem isso sobravam cantos
pretos. **Não medido:** o `python3-tkinter` não está instalado; sem ele o
app segue sem pílula (o ícone da bandeja muda de cor: verde, vermelho gravando, âmbar transcrevendo).

## 7. Suíte

`./tests/run_all.sh` = `pytest` sobre os mesmos `unittest.TestCase`. O `tests/_safety.py` ganhou a
trava do Linux: conectar no D-Bus de sessão, no Wayland ou no PipeWire do usuário, rodar
`wl-copy`/`wl-paste`/`xdg-open`/`ydotool`/o guardião, ou abrir `/dev/uinput` vira `RuntimeError`.
Os testes que exercitam a camada Win32 por dentro (struct do hook, `SendInput`, token de integridade,
`%PATH%`) pulam fora do Windows com o motivo escrito; o equivalente Linux está em
`tests/test_linux.py` — inclusive o protocolo do fake_input inteiro contra um compositor falso num
socket temporário.
