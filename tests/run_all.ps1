<#
.SYNOPSIS
    Roda a suite offline do wisper.

.DESCRIPTION
    Todos os testes sao offline e passivos: nao abrem microfone, nao carregam
    modelo, nao alocam VRAM, nao instalam hook de teclado, nao tocam no
    clipboard e nao abrem janela nenhuma. Pode rodar com o PC em uso, inclusive
    com jogo aberto.

.PARAMETER Python
    Interpretador a usar. Por padrao procura o venv do projeto, depois o
    CPython 3.11 do python.org, depois o `python` do PATH.

.PARAMETER Pattern
    Filtro de arquivos. Ex.: -Pattern test_dsp.py

.PARAMETER Quiet
    Saida resumida em vez de um teste por linha.

.EXAMPLE
    .\tests\run_all.ps1
    .\tests\run_all.ps1 -Pattern test_ring.py
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Pattern = "test_*.py",
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

# Devolve uma variavel de ambiente ao valor anterior, apagando se ela nao existia.
function Set-EnvOrClear {
    param([string] $Name, [string] $Value)

    $item = "Env:\" + $Name
    if ([string]::IsNullOrEmpty($Value)) {
        if (Test-Path -LiteralPath $item) {
            Remove-Item -LiteralPath $item -ErrorAction SilentlyContinue
        }
    } else {
        Set-Item -LiteralPath $item -Value $Value
    }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

# --- acha um interpretador ------------------------------------------------- #
$candidatos = @()
if ($Python) { $candidatos += $Python }
$candidatos += Join-Path $root ".venv\Scripts\python.exe"
$candidatos += "C:\Users\Administrador\AppData\Local\Programs\Python\Python311org\python.exe"

$py = $null
foreach ($c in $candidatos) {
    if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) {
    Write-Error "Nenhum Python encontrado. Rode scripts\setup.ps1 ou passe -Python <caminho>."
    exit 2
}

# O Python da Microsoft Store nao serve: ele redireciona escrita em %APPDATA% e
# tem consentimento de microfone proprio. Ver docs/ARCHITECTURE.md secao 4.
if ($py -like "*WindowsApps*") {
    Write-Warning "Esse Python e o da Microsoft Store. A suite roda, mas o app nao vai funcionar com ele."
}

Write-Host ""
Write-Host "wisper - suite offline" -ForegroundColor Cyan
Write-Host "  python : $py"
Write-Host "  raiz   : $root"
Write-Host "  seguro : sem microfone, sem GPU, sem hook, sem clipboard, sem janela" -ForegroundColor DarkGray
Write-Host ""

# --- roda ------------------------------------------------------------------ #
# As tres variaveis sao restauradas no finally: este script roda no console do
# usuario, e deixar PYTHONPATH apontando para a raiz do projeto azeda qualquer
# outro python que ele chame depois na mesma janela.
$oldPath  = $env:PYTHONPATH
$oldUtf8  = $env:PYTHONUTF8
$oldNoPyc = $env:PYTHONDONTWRITEBYTECODE

$env:PYTHONPATH = $root
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

# -t igual a -s poe a pasta tests/ no sys.path, que e o que faz o `import _safety`
# dos arquivos de teste funcionar sem virar pacote.
$pyArgs = @("-m", "unittest", "discover", "-s", $here, "-t", $here, "-p", $Pattern)
if (-not $Quiet) { $pyArgs += "-v" }

# Comeca em falha: se o `&` abaixo nem conseguir lancar o interpretador, o finally
# roda com $code ainda nao atribuido e `exit $null` sairia com 0 -- um verde falso.
$code = 1

Push-Location $root
try {
    & $py @pyArgs
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
}
catch {
    Write-Host ("nao consegui rodar o interpretador: " + $_.Exception.Message) -ForegroundColor Red
    $code = 2
}
finally {
    Pop-Location
    Set-EnvOrClear "PYTHONPATH" $oldPath
    Set-EnvOrClear "PYTHONUTF8" $oldUtf8
    Set-EnvOrClear "PYTHONDONTWRITEBYTECODE" $oldNoPyc
}

Write-Host ""
if ($code -eq 0) {
    Write-Host "OK" -ForegroundColor Green
} else {
    Write-Host "FALHOU (exit $code)" -ForegroundColor Red
}
exit $code
