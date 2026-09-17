<#
.SYNOPSIS
    Remove o wisper do logon.

.DESCRIPTION
    Sem argumentos: apaga o valor "wisper" de
    HKCU\Software\Microsoft\Windows\CurrentVersion\Run (nao precisa de elevacao).
    Com -Task: remove tambem a Tarefa Agendada elevada, o que EXIGE elevacao -
    apagar uma tarefa de privilegio mais alto de um shell comum da 'Acesso negado'.

    Este script nao encerra o processo em execucao: para isso use scripts\stop.ps1.

.PARAMETER Task
    Remove a Tarefa Agendada alem da entrada de registro.

.PARAMETER Name
    Nome da entrada / da tarefa. Padrao: wisper.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\autostart_uninstall.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\autostart_uninstall.ps1 -Task
#>
[CmdletBinding()]
param(
    [switch] $Task,
    [string] $Name = "wisper"
)

$ErrorActionPreference = "Stop"

$RUN_KEY = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Test-Elevated {
    try {
        $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object System.Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-WisperTask {
    param([string] $TaskName)
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return $null }
    try {
        return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

$Root    = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ThisPs1 = Join-Path $Root "scripts\autostart_uninstall.ps1"
$removed = 0

Write-Host ""

# ---------------------------------------------------- entrada HKCU\...\Run --

$entry = Get-ItemProperty -Path $RUN_KEY -Name $Name -ErrorAction SilentlyContinue
if ($null -eq $entry) {
    Write-Host "Entrada de registro '$Name': nao existe, nada a fazer." -ForegroundColor DarkGray
} else {
    try {
        Remove-ItemProperty -Path $RUN_KEY -Name $Name
        $removed = $removed + 1
        Write-Host "Entrada de registro '$Name' removida." -ForegroundColor Green
        Write-Host ("  era: {0}" -f $entry.$Name) -ForegroundColor DarkGray
    } catch {
        Write-Host ("ERRO ao remover a entrada de registro: " + $_.Exception.Message) -ForegroundColor Red
    }
}

# ------------------------------------------------------------ Tarefa Agendada --

# NAO chamar esta variavel de $task: nomes de variavel no PowerShell sao
# case-insensitive, entao $task E o mesmo slot do parametro [switch] $Task. A
# atribuicao passaria pelo conversor de tipo do parametro e o resultado seria um
# de dois desastres: com a tarefa existente, erro terminante ao converter o
# CimInstance para [switch]; sem ela, o -Task do usuario viraria $false calado.
$existingTask = Get-WisperTask -TaskName $Name

if (-not $Task) {
    if ($null -ne $existingTask) {
        Write-Host ""
        Write-Host "AVISO: ainda existe a Tarefa Agendada '$Name', e ela continua subindo" -ForegroundColor Yellow
        Write-Host "       o wisper no logon. Para remover tambem (num shell ELEVADO):" -ForegroundColor Yellow
        Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`" -Task" -f $ThisPs1) -ForegroundColor Green
    }
} else {
    if ($null -eq $existingTask) {
        Write-Host "Tarefa Agendada '$Name': nao existe, nada a fazer." -ForegroundColor DarkGray
    } elseif (-not (Test-Elevated)) {
        Write-Host ""
        Write-Host "A Tarefa Agendada '$Name' existe, mas remover precisa de elevacao." -ForegroundColor Yellow
        Write-Host "Ela foi criada com privilegio mais alto: de um shell comum, apagar" -ForegroundColor Yellow
        Write-Host "devolve 'Acesso negado'." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Abra um PowerShell COMO ADMINISTRADOR e rode:" -ForegroundColor White
        Write-Host ""
        Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`" -Task" -f $ThisPs1) -ForegroundColor Green
        Write-Host ""
        Write-Host "Ou peca o UAC daqui mesmo:" -ForegroundColor White
        Write-Host ("    Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy','Bypass','-File','{0}','-Task'" -f $ThisPs1) -ForegroundColor Green
        Write-Host ""
        exit 2
    } else {
        try {
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false
            $removed = $removed + 1
            Write-Host "Tarefa Agendada '$Name' removida." -ForegroundColor Green
        } catch {
            Write-Host ("ERRO ao remover a tarefa: " + $_.Exception.Message) -ForegroundColor Red
            Write-Host "Alternativa manual, elevado:  schtasks /Delete /TN $Name /F" -ForegroundColor DarkGray
        }
    }
}

Write-Host ""
if ($removed -gt 0) {
    Write-Host "O wisper nao sobe mais sozinho no logon." -ForegroundColor White
    Write-Host "A instancia que ja esta rodando continua viva ate voce parar:" -ForegroundColor DarkGray
    Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}\scripts\stop.ps1`"" -f $Root) -ForegroundColor DarkGray
} else {
    Write-Host "Nada foi removido." -ForegroundColor DarkGray
}
Write-Host ""

exit 0
