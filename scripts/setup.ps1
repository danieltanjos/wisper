<#
.SYNOPSIS
    Setup de primeira vez do wisper. Idempotente: pode rodar quantas vezes quiser.

.DESCRIPTION
    Valida o interpretador, cria o .venv, instala o requirements.txt, prepara
    models/ logs/ assets/ e o config.json, e opcionalmente pre-baixa o modelo.

    Recusa o Python da Microsoft Store. Nao e frescura: o build da Store tem
    identidade de pacote e o InputStream() do microfone trava para sempre sem
    excecao. Ver docs/ARCHITECTURE.md secao 4.

.PARAMETER Python
    Interpretador base. Tem que ser python.org CPython 3.11 x64.

.PARAMETER DownloadModel
    Baixa agora os pesos do Whisper (~1,6 GB) para dentro de models/.
    Sem esse switch o download acontece sozinho no primeiro ditado.

.PARAMETER Force
    Apaga e recria o .venv do zero.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -DownloadModel
#>
[CmdletBinding()]
param(
    [string] $Python = "C:\Users\Administrador\AppData\Local\Programs\Python\Python311org\python.exe",
    [switch] $DownloadModel,
    [switch] $Force
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- utilidades --

function Write-Step {
    param([string] $Msg)
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

function Write-Ok {
    param([string] $Msg)
    Write-Host "    [ok]  $Msg" -ForegroundColor Green
}

function Write-Note {
    param([string] $Msg)
    Write-Host "    [!]   $Msg" -ForegroundColor Yellow
}

function Write-Info {
    param([string] $Msg)
    Write-Host "          $Msg" -ForegroundColor DarkGray
}

function Write-Fail {
    param([string]$Msg)
    Write-Host ""
    Write-Host "ERRO: $Msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Invoke-PySnippet {
    <#
        Roda um trecho de Python num arquivo temporario em vez de mandar por stdin:
        no Windows PowerShell 5.1 o $OutputEncoding do pipe para exe nativo e ASCII
        e corromperia qualquer acento. %TEMP% e seguro ate para o Python da Store
        (ele so redireciona %LOCALAPPDATA% e %APPDATA%).
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Exe,
        [Parameter(Mandatory = $true)][string] $Code
    )
    $tmp = Join-Path $env:TEMP ("wisper_setup_" + [guid]::NewGuid().ToString("N") + ".py")
    try {
        Set-Content -LiteralPath $tmp -Value $Code -Encoding UTF8
        $out = & $Exe $tmp
        $rc = $LASTEXITCODE
        return [pscustomobject]@{ Lines = @($out); Code = $rc }
    } catch {
        return [pscustomobject]@{ Lines = @(); Code = 9009 }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function ConvertFrom-KeyValueLines {
    param([string[]] $Lines)
    $h = @{}
    foreach ($line in $Lines) {
        $s = [string]$line
        $i = $s.IndexOf("=")
        if ($i -gt 0) { $h[$s.Substring(0, $i).Trim()] = $s.Substring($i + 1).Trim() }
    }
    return $h
}

function Get-DirSizeMB {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [math]::Round($sum / 1MB, 1)
}

# O probe precisa rodar em qualquer 3.x, entao nada de f-string aqui.
$PROBE_CODE = @'
import sys
print("major=%d" % sys.version_info[0])
print("minor=%d" % sys.version_info[1])
print("micro=%d" % sys.version_info[2])
print("bits=%d" % (64 if sys.maxsize > 2 ** 32 else 32))
print("base_prefix=%s" % sys.base_prefix)
print("prefix=%s" % sys.prefix)
print("executable=%s" % sys.executable)
'@

function Test-StorePython {
    param([hashtable] $Info)
    foreach ($key in @("base_prefix", "prefix", "executable")) {
        $v = [string]$Info[$key]
        if ($v -match "WindowsApps") { return $true }
        if ($v -match "PythonSoftwareFoundation") { return $true }
    }
    return $false
}

function Deny-StorePython {
    param([string] $Exe, [hashtable] $Info)
    Write-Host ""
    Write-Host "ERRO: este interpretador e o Python da Microsoft Store." -ForegroundColor Red
    Write-Host ""
    Write-Host "  executavel : $Exe"
    Write-Host "  base_prefix: $($Info['base_prefix'])"
    Write-Host ""
    Write-Host "O build da Store tem identidade de pacote e uma entrada propria no" -ForegroundColor Yellow
    Write-Host "ConsentStore do microfone. Na pratica, medido nesta maquina:" -ForegroundColor Yellow
    Write-Host "  - com a permissao em 'Prompt', o InputStream() trava para sempre e" -ForegroundColor Yellow
    Write-Host "    nao levanta excecao nenhuma (espera uma UI que nunca aparece);" -ForegroundColor Yellow
    Write-Host "  - depois vira 'Deny' e falha com -9999/-9996 em todo device;" -ForegroundColor Yellow
    Write-Host "  - e ele redireciona em silencio as escritas em %LOCALAPPDATA% e" -ForegroundColor Yellow
    Write-Host "    %APPDATA% para o sandbox do pacote." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Instale o CPython 3.11 x64 do python.org e rode de novo apontando para ele:"
    Write-Host "  https://www.python.org/downloads/release/python-3119/"
    Write-Host ""
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 ``"
    Write-Host "      -Python `"C:\Users\Administrador\AppData\Local\Programs\Python\Python311org\python.exe`""
    Write-Host ""
    Write-Host "Detalhes: docs/ARCHITECTURE.md secao 4." -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

# -------------------------------------------------------------------- inicio --

$Root      = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Req       = Join-Path $Root "requirements.txt"
$VenvDir   = Join-Path $Root ".venv"
$VenvPy    = Join-Path $VenvDir "Scripts\python.exe"
$VenvPyw   = Join-Path $VenvDir "Scripts\pythonw.exe"
$ModelsDir = Join-Path $Root "models"
$LogDir    = Join-Path $Root "logs"
$AssetsDir = Join-Path $Root "assets"
$MainPyw   = Join-Path $Root "main.pyw"
$RunPs1    = Join-Path $Root "scripts\run.ps1"

Write-Host ""
Write-Host "  wisper - setup" -ForegroundColor White
Write-Host "  projeto: $Root" -ForegroundColor DarkGray

if (-not (Test-Path -LiteralPath $Req)) {
    Write-Fail "requirements.txt nao encontrado em $Req. Rode este script de dentro do repo."
}

# 1 --------------------------------------------------------- interpretador base

Write-Step "Verificando o interpretador base"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host ""
    Write-Host "ERRO: interpretador nao encontrado:" -ForegroundColor Red
    Write-Host "  $Python"
    Write-Host ""
    Write-Host "Instale o CPython 3.11 x64 do python.org (NAO o da Microsoft Store) ou"
    Write-Host "passe o caminho certo com -Python:"
    Write-Host "  .\scripts\setup.ps1 -Python `"C:\caminho\para\python.exe`""
    Write-Host ""
    exit 1
}

$probe = Invoke-PySnippet -Exe $Python -Code $PROBE_CODE
if ($probe.Code -ne 0) {
    Write-Fail "nao consegui executar '$Python' (codigo $($probe.Code))."
}
$info = ConvertFrom-KeyValueLines -Lines $probe.Lines

if (Test-StorePython -Info $info) { Deny-StorePython -Exe $Python -Info $info }

if ($info["major"] -ne "3" -or $info["minor"] -ne "11") {
    Write-Fail ("preciso de Python 3.11, achei $($info['major']).$($info['minor']).$($info['micro']) em $Python. " +
                "O projeto so foi validado no 3.11 (ctranslate2 4.8.2 + faster-whisper 1.2.1).")
}
if ($info["bits"] -ne "64") {
    Write-Fail "preciso de Python x64; este e de $($info['bits']) bits. As wheels de CUDA so existem em x64."
}

Write-Ok "python.org CPython $($info['major']).$($info['minor']).$($info['micro']) x64"
Write-Info $info["executable"]

# 2 ------------------------------------------------------------------- o .venv

Write-Step "Ambiente virtual"

if ($Force -and (Test-Path -LiteralPath $VenvDir)) {
    Write-Note "-Force: apagando o .venv existente"
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
}

if (Test-Path -LiteralPath $VenvPy) {
    # Idempotencia com armadilha: um .venv antigo pode ter sido criado a partir do
    # Python da Store. Ele nao se conserta sozinho, entao checamos a origem.
    $vprobe = Invoke-PySnippet -Exe $VenvPy -Code $PROBE_CODE
    if ($vprobe.Code -ne 0) {
        Write-Fail "o .venv existente esta quebrado. Rode de novo com -Force para recria-lo."
    }
    $vinfo = ConvertFrom-KeyValueLines -Lines $vprobe.Lines
    if (Test-StorePython -Info $vinfo) {
        Write-Note "o .venv existente foi criado a partir do Python da Microsoft Store."
        Deny-StorePython -Exe $VenvPy -Info $vinfo
    }
    if ($vinfo["minor"] -ne "11") {
        Write-Fail "o .venv existente e 3.$($vinfo['minor']). Rode de novo com -Force para recria-lo em 3.11."
    }
    Write-Ok "reaproveitando o .venv ja existente"
} else {
    Write-Info "criando $VenvDir ..."
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Fail "falha ao criar o .venv (codigo $LASTEXITCODE)." }
    if (-not (Test-Path -LiteralPath $VenvPy)) { Write-Fail "o .venv foi criado sem Scripts\python.exe." }
    Write-Ok ".venv criado"
}
Write-Info $VenvPy

# 3 ---------------------------------------------------------------- dependencias

Write-Step "Atualizando o pip"
& $VenvPy -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Write-Fail "falha ao atualizar o pip (codigo $LASTEXITCODE)." }
Write-Ok "pip atualizado"

Write-Step "Instalando requirements.txt (demora alguns minutos na primeira vez)"
& $VenvPy -m pip install --disable-pip-version-check -r $Req
if ($LASTEXITCODE -ne 0) {
    Write-Fail ("falha no pip install (codigo $LASTEXITCODE). Se foi timeout de rede, basta rodar " +
                "o setup de novo: o pip reaproveita o que ja baixou.")
}
Write-Ok "dependencias instaladas"

# 4 ------------------------------------------------------------------ verificacao

Write-Step "Conferindo o que foi instalado"

# find_spec NAO executa o modulo: nada de abrir microfone, alocar VRAM ou subir
# janela so para verificar a instalacao.
$VERIFY_CODE = @'
import glob
import importlib.util
import os

# CORE: o que wispr/ realmente importa. Faltando qualquer um, o app nao sobe.
# _tkinter junto com tkinter de proposito: quem roda o instalador do python.org
# sem "tcl/tk and IDLE" fica com o tkinter/ (.py) no lugar e sem o _tkinter.pyd,
# e ai o overlay morre no primeiro import - invisivel sob pythonw.exe.
core = ["faster_whisper", "ctranslate2", "sounddevice", "soxr", "comtypes",
        "numpy", "scipy", "pystray", "PIL", "tkinter", "_tkinter",
        "huggingface_hub"]
# EXTRA: instalados pelo requirements mas ainda nao importados por ninguem.
extra = ["soundfile", "pycaw", "webrtcvad"]


def absent(names):
    out = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
        except Exception:
            out.append(name)
    return out


print("MISSING=" + ",".join(absent(core)))
print("MISSING_EXTRA=" + ",".join(absent(extra)))

# As DLLs de CUDA que o ctranslate2 carrega em runtime, fora da import table.
# nvidia e namespace package: nao existe nvidia.__file__ (e None) e o caminho de
# site-packages nem sempre e <prefix>\Lib\site-packages. O spec do find_spec da o
# mesmo que o nvidia.__path__ que o wispr/stt.py usa, e sem importar nada.
bins = []
try:
    spec = importlib.util.find_spec("nvidia")
    roots = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
except Exception:
    roots = []
for root in roots:
    bins.extend(glob.glob(os.path.join(root, "*", "bin")))
print("CUDA_DIRS=%d" % len(bins))
print("CUBLAS=%d" % int(any(os.path.exists(os.path.join(b, "cublas64_12.dll")) for b in bins)))
print("CUDNN=%d" % int(any(glob.glob(os.path.join(b, "cudnn*.dll")) for b in bins)))
'@

$ver = Invoke-PySnippet -Exe $VenvPy -Code $VERIFY_CODE
$vres = ConvertFrom-KeyValueLines -Lines $ver.Lines
$cudaOk = $false

if ($ver.Code -ne 0 -or -not $vres.ContainsKey("MISSING")) {
    # ContainsKey e nao apenas o codigo de saida: um python que sai 0 sem imprimir
    # nada cairia no ramo de sucesso e diria "todos os modulos presentes" sem ter
    # verificado coisa alguma.
    Write-Note "nao consegui rodar a verificacao; siga em frente e olhe logs\wisper.log depois."
} else {
    $missing = [string]$vres["MISSING"]
    if ($missing.Length -gt 0) {
        Write-Note "pacotes ESSENCIAIS faltando: $missing"
        Write-Info "o app nao sobe assim. Rode o setup de novo; se persistir, veja o erro do pip acima."
        if ($missing -match "_?tkinter") {
            Write-Info "tkinter/_tkinter vem do instalador do python.org, nao do pip: reinstale o"
            Write-Info "Python marcando 'tcl/tk and IDLE'. Sem ele o overlay nao existe."
        }
    } else {
        Write-Ok "todos os modulos essenciais presentes"
    }
    $missingExtra = [string]$vres["MISSING_EXTRA"]
    if ($missingExtra.Length -gt 0) {
        Write-Note "reserva faltando: $missingExtra"
        Write-Info "nenhum modulo importa esses hoje; o app funciona sem eles."
    }
    if ($vres["CUBLAS"] -eq "1" -and $vres["CUDNN"] -eq "1") {
        $cudaOk = $true
        Write-Ok "wheels de CUDA ok (cublas64_12.dll e cudnn*.dll em nvidia\*\bin)"
    } else {
        Write-Note "cublas64_12.dll ou cudnn*.dll nao apareceram em nvidia\*\bin."
        Write-Info "o modelo vai carregar em 'cuda' e so o transcribe() vai estourar com"
        Write-Info "'Library cublas64_12.dll is not found'. O app cai para CPU sozinho,"
        Write-Info "mas fica ~20x mais lento. Confira o pip install acima."
    }
}

# 5 -------------------------------------------------------- pastas e config.json

Write-Step "Pastas e config.json"

# Criadas aqui tambem, e nao so pelo config.ensure_dirs(), para que o resto do
# setup funcione mesmo se o snippet Python abaixo falhar.
foreach ($d in @($ModelsDir, $LogDir, $AssetsDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Write-Ok "models\ logs\ assets\ prontas"

$env:WISPER_ROOT = $Root

$CONFIG_CODE = @'
import json
import os
import sys

sys.path.insert(0, os.environ["WISPER_ROOT"])
from wispr import config

config.ensure_dirs()
path = config.CONFIG_PATH
if path.exists():
    print("CONFIG=kept")
else:
    # Grava o DEFAULTS inteiro, e nao o diff do config.save(), para o arquivo
    # servir de referencia editavel de todas as chaves.
    path.write_text(json.dumps(config.DEFAULTS, indent=2, ensure_ascii=False), encoding="utf-8")
    print("CONFIG=created")
cfg = config.load()
print("PATH=" + str(path))
print("MODELS=" + str(config.MODELS_DIR))
print("MODEL_ID=" + str(cfg["model_id"]))
print("ENGINE=" + str(cfg["engine"]))
print("HOTKEY=" + str(cfg["hotkey"]))
'@

$cfgRes  = Invoke-PySnippet -Exe $VenvPy -Code $CONFIG_CODE
$cfgInfo = ConvertFrom-KeyValueLines -Lines $cfgRes.Lines
$modelId = "deepdml/faster-whisper-large-v3-turbo-ct2"

if ($cfgRes.Code -ne 0) {
    Write-Note "nao consegui ler o wispr\config.py; usando os valores padrao no resumo."
} else {
    if ($cfgInfo["CONFIG"] -eq "created") { Write-Ok "config.json criado a partir dos DEFAULTS" }
    else { Write-Ok "config.json ja existia, preservado" }
    Write-Info $cfgInfo["PATH"]
    if ($cfgInfo["MODEL_ID"]) { $modelId = $cfgInfo["MODEL_ID"] }
}

# 6 ------------------------------------------------------------------- o modelo

$modelMB = Get-DirSizeMB -Path $ModelsDir

if ($DownloadModel) {
    Write-Step "Baixando o modelo (~1,6 GB, so na primeira vez)"

    # HF_HOME explicito de proposito: o config.py usa os.environ.setdefault, entao
    # um HF_HOME herdado do shell do usuario venceria e os pesos iriam para o
    # cache global em vez de models\.
    $env:HF_HOME = $ModelsDir
    $env:HF_HUB_DISABLE_TELEMETRY = "1"

    $DOWNLOAD_CODE = @'
import os
import sys

sys.path.insert(0, os.environ["WISPER_ROOT"])
from wispr import config  # seta HF_HOME antes de qualquer import do huggingface_hub

from huggingface_hub import snapshot_download

cfg = config.load()
repo = cfg["model_id"]
# Mesmo conjunto de arquivos que o faster_whisper.download_model busca. O resto do
# repo (pesos .pt originais do Whisper) e inutil para o CTranslate2 e so ocupa disco.
patterns = ["config.json", "preprocessor_config.json", "model.bin",
            "tokenizer.json", "vocabulary.*"]
try:
    path = snapshot_download(repo_id=repo, allow_patterns=patterns)
except Exception as exc:
    print("ERROR=%s: %s" % (type(exc).__name__, exc))
    sys.exit(2)
print("MODEL_PATH=" + str(path))
'@

    $dl = Invoke-PySnippet -Exe $VenvPy -Code $DOWNLOAD_CODE
    $dlInfo = ConvertFrom-KeyValueLines -Lines $dl.Lines
    if ($dl.Code -ne 0) {
        # O snippet so imprime ERROR= quando ele mesmo pegou a excecao. Se o python
        # morreu antes disso, a chave nao existe e a mensagem sairia vazia.
        $why = [string]$dlInfo["ERROR"]
        if ($why.Length -eq 0) { $why = "o python saiu com codigo $($dl.Code) sem dizer por que" }
        Write-Note "o download falhou: $why"
        Write-Info "nao e fatal: o modelo baixa sozinho no primeiro ditado."
    } else {
        $modelMB = Get-DirSizeMB -Path $ModelsDir
        Write-Ok "modelo em cache ($modelMB MB)"
        Write-Info $dlInfo["MODEL_PATH"]
    }
} else {
    if ($modelMB -gt 500) {
        Write-Step "Modelo"
        Write-Ok "models\ ja tem $modelMB MB em cache"
    } else {
        Write-Step "Modelo"
        Write-Note "os pesos (~1,6 GB) ainda nao foram baixados."
        Write-Info "baixa sozinho no primeiro Win+A, ou agora com:"
        Write-Info "  .\scripts\setup.ps1 -DownloadModel"
    }
}

# 7 -------------------------------------------------------------------- resumo

$modelMB = Get-DirSizeMB -Path $ModelsDir

Write-Host ""
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  RESUMO" -ForegroundColor White
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ("  projeto      : {0}" -f $Root)
Write-Host ("  interpretador: CPython {0}.{1}.{2} x64 (python.org)" -f $info["major"], $info["minor"], $info["micro"])
Write-Host ("  venv         : {0}" -f $VenvDir)
Write-Host ("  config       : {0}" -f (Join-Path $Root "config.json"))
Write-Host ("  logs         : {0}" -f (Join-Path $LogDir "wisper.log"))
Write-Host ("  modelo       : {0} ({1} MB em cache)" -f $modelId, $modelMB)
if ($cudaOk) {
    Write-Host "  CUDA         : wheels ok (int8_float16, ~1024 MB de VRAM)"
} else {
    Write-Host "  CUDA         : INCOMPLETA - o app vai cair para CPU" -ForegroundColor Yellow
}
if (-not (Test-Path -LiteralPath $MainPyw)) {
    Write-Host "  main.pyw     : AINDA NAO EXISTE - o app nao sobe sem ele" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  PROXIMO PASSO (primeira vez, com os erros visiveis no console):" -ForegroundColor White
Write-Host ""
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$RunPs1`" -Console" -ForegroundColor Green
Write-Host ""
Write-Host "  Depois, para rodar em segundo plano (sem console):" -ForegroundColor White
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$RunPs1`""
Write-Host ""
Write-Host "  Outros comandos:" -ForegroundColor White
Write-Host ("    parar          : .\scripts\stop.ps1")
Write-Host ("    iniciar no logon: .\scripts\autostart_install.ps1")
Write-Host ("    remover do logon: .\scripts\autostart_uninstall.ps1")
Write-Host ""
Write-Host "  Uso: Win+A grava, Enter finaliza e digita, Esc cancela." -ForegroundColor DarkGray
Write-Host ""

exit 0
