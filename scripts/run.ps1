<#
.SYNOPSIS
    Sobe o wisper.

.DESCRIPTION
    Por padrao lanca main.pyw com o pythonw.exe do .venv, destacado: nao abre
    console e sobrevive ao fechamento deste shell. Sob pythonw.exe nao existe
    stdout nem stderr, entao TUDO vai parar em logs\wisper.log.

.PARAMETER Console
    Usa python.exe e roda em primeiro plano, neste console, com os erros visiveis.
    E o modo de depuracao. Ctrl+C encerra.

.PARAMETER Force
    Sobe mesmo que ja exista uma instancia rodando (o mutex do app ainda vai
    barrar a segunda; serve para casos de processo zumbi).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Console

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
#>
[CmdletBinding()]
param(
    [switch] $Console,
    [switch] $Force
)

$ErrorActionPreference = "Stop"

function Write-Fail {
    param([string] $Msg)
    Write-Host ""
    Write-Host "ERRO: $Msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Get-WisperProcess {
    <#
        Acha instancias do wisper pela linha de comando. Win32_Process.CommandLine
        vem $null para processos de outro usuario ou mais privilegiados: a instancia
        elevada (autostart_install.ps1 -Task) pode nao aparecer aqui.
    #>
    param([string] $Root)
    $found = @()
    try {
        $all = Get-CimInstance -ClassName Win32_Process `
                               -Filter "Name='pythonw.exe' OR Name='python.exe'" `
                               -ErrorAction SilentlyContinue
    } catch {
        return @()
    }
    foreach ($p in $all) {
        $cl = [string]$p.CommandLine
        if ($cl.Length -eq 0) { continue }
        if ($cl.IndexOf("main.pyw", [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        if ($cl.IndexOf($Root, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        $found += $p
    }
    return $found
}

$Root      = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir   = Join-Path $Root ".venv"
$VenvPy    = Join-Path $VenvDir "Scripts\python.exe"
$VenvPyw   = Join-Path $VenvDir "Scripts\pythonw.exe"
$MainPyw   = Join-Path $Root "main.pyw"
$ModelsDir = Join-Path $Root "models"
$LogFile   = Join-Path $Root "logs\wisper.log"
$SetupPs1  = Join-Path $Root "scripts\setup.ps1"
# Dois arquivos de crash, e nenhum deles e o wisper.log: o main.pyw grava
# crash.log quando a subida morre antes do logging existir, e o logging_setup
# arma o faulthandler no wisper-crash.log para queda dura de DLL nativa. Numa
# falha de startup sob pythonw.exe eles costumam ser a unica prova que sobra.
$CrashFile = Join-Path $Root "logs\crash.log"
$FaultFile = Join-Path $Root "logs\wisper-crash.log"

if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Fail ("o .venv nao existe em $VenvDir. Rode primeiro:`r`n" +
                "  powershell -ExecutionPolicy Bypass -File `"$SetupPs1`"")
}
if (-not (Test-Path -LiteralPath $MainPyw)) {
    Write-Fail "main.pyw nao encontrado em $MainPyw."
}
if (-not (Test-Path -LiteralPath $VenvPyw) -and -not $Console) {
    Write-Fail ("pythonw.exe nao existe no .venv. Use -Console ou recrie o ambiente:`r`n" +
                "  powershell -ExecutionPolicy Bypass -File `"$SetupPs1`" -Force")
}

$running = @(Get-WisperProcess -Root $Root)
if ($running.Count -gt 0 -and -not $Force) {
    Write-Host ""
    Write-Host "O wisper ja parece estar rodando:" -ForegroundColor Yellow
    foreach ($p in $running) {
        Write-Host ("  PID {0}  ({1})" -f $p.ProcessId, $p.Name)
    }
    Write-Host ""
    Write-Host 'Ele so aceita uma instancia (mutex "Local\WisprCloneDaniel").'
    Write-Host 'Para reiniciar:  .\scripts\stop.ps1 ; if ($?) { .\scripts\run.ps1 }'
    Write-Host 'Para ignorar:    .\scripts\run.ps1 -Force'
    Write-Host ""
    exit 0
}

# HF_HOME explicito: o config.py usa os.environ.setdefault, entao um HF_HOME que o
# usuario ja tenha no ambiente venceria e o modelo seria procurado no cache global
# em vez de models\ (e baixado de novo, 1,6 GB).
$env:HF_HOME = $ModelsDir
$env:HF_HUB_DISABLE_TELEMETRY = "1"
# Evita UnicodeEncodeError em print de acento quando o console esta em cp1252.
$env:PYTHONUTF8 = "1"

if ($Console) {
    Write-Host ""
    Write-Host "wisper em modo console (python.exe, primeiro plano)." -ForegroundColor Cyan
    Write-Host "  log : $LogFile" -ForegroundColor DarkGray
    Write-Host "  Win+A grava, Enter finaliza e digita, Esc cancela. Ctrl+C encerra." -ForegroundColor DarkGray
    Write-Host ""
    & $VenvPy $MainPyw
    $rc = $LASTEXITCODE
    Write-Host ""
    if ($rc -eq 0) {
        Write-Host "wisper encerrou normalmente." -ForegroundColor Green
    } else {
        Write-Host "wisper encerrou com codigo $rc." -ForegroundColor Yellow
        Write-Host "  log   : $LogFile" -ForegroundColor Yellow
        if (Test-Path -LiteralPath $CrashFile) { Write-Host "  crash : $CrashFile" -ForegroundColor Yellow }
        if (Test-Path -LiteralPath $FaultFile) { Write-Host "  nativo: $FaultFile" -ForegroundColor Yellow }
    }
    exit $rc
}

# Destacado: Start-Process nao cria vinculo de job com este shell, entao o processo
# continua vivo quando a janela do PowerShell fecha.
try {
    $proc = Start-Process -FilePath $VenvPyw `
                          -ArgumentList ("`"$MainPyw`"") `
                          -WorkingDirectory $Root `
                          -WindowStyle Hidden `
                          -PassThru
} catch {
    # Sem este catch o $ErrorActionPreference = "Stop" despeja a excecao crua do
    # PowerShell na cara do usuario, que nao diz o que fazer.
    Write-Fail ("nao consegui lancar o pythonw.exe: " + $_.Exception.Message)
}

if ($null -eq $proc) { Write-Fail "Start-Process nao devolveu processo nenhum." }

# Confirmacao de que ele sobreviveu ao startup. Sob pythonw.exe nao ha stdout nem
# stderr: um traceback na subida mata o processo em silencio e sem esta espera o
# script anunciaria "iniciado" para algo que ja morreu. 3 s cobre o caminho ate o
# mutex e a bandeja; o carregamento do modelo (preload_model) segue depois disso.
$died = $false
Start-Sleep -Seconds 3
try {
    $proc.Refresh()
    $died = $proc.HasExited
} catch {
    # Processo ja coletado ou handle perdido: cai no plano B por PID.
    $died = ($null -eq (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue))
}

if ($died) {
    $rc = "desconhecido"
    try { $rc = [string]$proc.ExitCode } catch { }

    if ($running.Count -gt 0) {
        # Caminho do -Force: a segunda copia bate no mutex e sai calada, de
        # proposito (wispr/app.py: "segunda copia sai calada"). Isso nao e falha,
        # e nao vale mandar o usuario cacar um crash.log que nao existe.
        Write-Host ""
        Write-Host ("A segunda instancia (PID {0}) saiu no mutex, como esperado." -f $proc.Id) -ForegroundColor Yellow
        Write-Host "A que ja estava rodando continua viva e e ela que atende o Win+A." -ForegroundColor DarkGray
        Write-Host "Para trocar de verdade:  .\scripts\stop.ps1  e depois  .\scripts\run.ps1" -ForegroundColor DarkGray
        Write-Host ""
        exit 0
    }

    Write-Host ""
    Write-Host ("O wisper subiu e morreu em menos de 3 s (PID {0}, codigo {1})." -f $proc.Id, $rc) -ForegroundColor Red
    Write-Host ""
    Write-Host "Onde esta o motivo, nesta ordem:" -ForegroundColor Yellow
    if (Test-Path -LiteralPath $CrashFile) {
        Write-Host ("  1. {0}   (o main.pyw grava aqui o traceback da subida)" -f $CrashFile)
    } else {
        Write-Host ("  1. {0}   (ainda nao existe)" -f $CrashFile)
    }
    Write-Host ("  2. {0}" -f $LogFile)
    if (Test-Path -LiteralPath $FaultFile) { Write-Host ("  3. {0}   (queda dura em DLL nativa)" -f $FaultFile) }
    Write-Host ""
    Write-Host "Ou rode em primeiro plano, que ai o traceback aparece aqui mesmo:" -ForegroundColor White
    Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`" -Console" -f $PSCommandPath) -ForegroundColor Green
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host ("wisper iniciado em segundo plano (PID {0})." -f $proc.Id) -ForegroundColor Green
Write-Host "  icone   : bandeja do sistema, perto do relogio" -ForegroundColor DarkGray
Write-Host "  log     : $LogFile" -ForegroundColor DarkGray
Write-Host "  parar   : .\scripts\stop.ps1" -ForegroundColor DarkGray
Write-Host ""
Write-Host "O icone pode demorar mais alguns segundos: com preload_model=true o" -ForegroundColor DarkGray
Write-Host "modelo carrega antes do primeiro Win+A. Se nunca aparecer, olhe" -ForegroundColor DarkGray
Write-Host "$LogFile e $CrashFile, ou rode de novo com -Console." -ForegroundColor DarkGray
Write-Host ""

exit 0
