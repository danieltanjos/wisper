<#
.SYNOPSIS
    Para a instancia do wisper que estiver rodando.

.DESCRIPTION
    Acha os processos python.exe/pythonw.exe cuja linha de comando aponta para o
    main.pyw deste projeto e encerra.

    Primeiro tenta o caminho educado (taskkill sem /F, que manda WM_CLOSE para as
    janelas do processo). Isso nem sempre pega: o app vive de pythonw.exe, a
    janela do pystray e oculta e a do overlay e WS_EX_NOACTIVATE, entao o WM_CLOSE
    pode simplesmente nao ser atendido. Por isso, passado o timeout, o script
    escala para /F e avisa. O mutex "Local\WisprCloneDaniel" e liberado pelo
    proprio Windows quando o processo morre, inclusive no /F.

    O jeito mais limpo de todos continua sendo "Sair" no menu do icone da bandeja.

.PARAMETER TimeoutSec
    Quanto esperar o encerramento educado antes de forcar. Padrao: 8.

.PARAMETER Force
    Vai direto no /F, sem tentar o WM_CLOSE.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1 -Force
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]    $TimeoutSec = 8,
    [switch] $Force
)

$ErrorActionPreference = "Stop"

function Get-WisperProcess {
    <#
        Win32_Process.CommandLine vem $null quando o processo pertence a outro
        usuario ou roda elevado e este shell nao esta: uma instancia criada pela
        Tarefa Agendada (autostart_install.ps1 -Task) so aparece aqui se este
        shell tambem estiver elevado.
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

function Test-ProcessAlive {
    param([int] $ProcessId)
    $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    return ($null -ne $p)
}

function Invoke-Taskkill {
    param([int] $ProcessId, [switch] $Hard)
    try {
        if ($Hard) { $null = & taskkill.exe /PID $ProcessId /T /F }
        else       { $null = & taskkill.exe /PID $ProcessId /T }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$Root     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunPs1   = Join-Path $Root "scripts\run.ps1"
$LogFile  = Join-Path $Root "logs\wisper.log"

$targets = @(Get-WisperProcess -Root $Root)

Write-Host ""
if ($targets.Count -eq 0) {
    Write-Host "Nenhuma instancia do wisper rodando." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Se o icone ainda esta na bandeja, e so um icone fantasma: passe o mouse" -ForegroundColor DarkGray
    Write-Host "por cima que ele some. Se o processo foi criado elevado (Tarefa Agendada)," -ForegroundColor DarkGray
    Write-Host "rode este script num PowerShell como administrador para enxerga-lo." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host ("Para subir:  powershell -ExecutionPolicy Bypass -File `"{0}`"" -f $RunPs1) -ForegroundColor DarkGray
    Write-Host ""
    exit 0
}

Write-Host ("Instancias encontradas: {0}" -f $targets.Count) -ForegroundColor Cyan
foreach ($p in $targets) {
    Write-Host ("  PID {0}  {1}" -f $p.ProcessId, $p.Name) -ForegroundColor DarkGray
}
Write-Host ""

$stillAlive = @()

foreach ($p in $targets) {
    $procId = [int]$p.ProcessId

    # Quem decide se deu certo e o processo, nunca o codigo de saida do taskkill:
    # se ele morreu sozinho entre a checagem e o kill, o taskkill devolve 128
    # ("no running instance") e o script acusaria falha num processo ja morto.
    if ($Force) {
        Write-Host ("  PID {0}: -Force, encerrando na marra ..." -f $procId)
        $null = Invoke-Taskkill -ProcessId $procId -Hard
        Start-Sleep -Milliseconds 400
        if (Test-ProcessAlive -ProcessId $procId) { $stillAlive += $procId }
        continue
    }

    Write-Host ("  PID {0}: pedindo encerramento (WM_CLOSE) ..." -f $procId)
    $polite = Invoke-Taskkill -ProcessId $procId

    # O taskkill sem /F devolve erro na hora ("can only be terminated forcefully")
    # quando o processo nao tem janela de topo que aceite WM_CLOSE - que e
    # exatamente o caso aqui: pythonw.exe, janela do pystray oculta e overlay
    # WS_EX_NOACTIVATE. Nesse caso esperar os 8 s inteiros e so tempo perdido,
    # entao a carencia cai para 2 s antes de escalar.
    $grace = $TimeoutSec
    if (-not $polite) {
        $grace = [Math]::Min($TimeoutSec, 2)
        Write-Host "         (o WM_CLOSE nao foi aceito; escalando em $grace s)" -ForegroundColor DarkGray
    }

    $deadline = (Get-Date).AddSeconds($grace)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-ProcessAlive -ProcessId $procId)) { break }
        Start-Sleep -Milliseconds 250
    }

    if (Test-ProcessAlive -ProcessId $procId) {
        Write-Host ("  PID {0}: nao respondeu em {1}s, forcando (/F) ..." -f $procId, $grace) -ForegroundColor Yellow
        $null = Invoke-Taskkill -ProcessId $procId -Hard
        Start-Sleep -Milliseconds 400
        if (Test-ProcessAlive -ProcessId $procId) { $stillAlive += $procId }
    } else {
        Write-Host ("  PID {0}: encerrado." -f $procId) -ForegroundColor Green
    }
}

Write-Host ""
if ($stillAlive.Count -gt 0) {
    Write-Host ("Nao consegui encerrar: {0}" -f ($stillAlive -join ", ")) -ForegroundColor Red
    Write-Host ""
    Write-Host "Quase sempre e questao de privilegio: um processo elevado nao pode ser" -ForegroundColor Yellow
    Write-Host "encerrado por um shell comum. Abra um PowerShell como administrador e" -ForegroundColor Yellow
    Write-Host "rode de novo com -Force." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host "wisper parado. O mutex foi liberado; ja pode subir de novo." -ForegroundColor Green
Write-Host ("  log    : {0}" -f $LogFile) -ForegroundColor DarkGray
Write-Host ("  subir  : powershell -ExecutionPolicy Bypass -File `"{0}`"" -f $RunPs1) -ForegroundColor DarkGray
Write-Host ""

exit 0
