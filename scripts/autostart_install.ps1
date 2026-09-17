<#
.SYNOPSIS
    Faz o wisper subir sozinho no logon.

.DESCRIPTION
    Por padrao cria o valor "wisper" em
    HKCU\Software\Microsoft\Windows\CurrentVersion\Run, apontando para o
    pythonw.exe do .venv com o main.pyw. Essa chave e gravavel sem elevacao
    (verificado nesta maquina).

    Com -Task cria uma Tarefa Agendada ONLOGON com privilegio mais alto, que e o
    unico jeito de o Win+A continuar funcionando quando uma janela ELEVADA esta
    em foco. Detalhe importante e invisivel: por UIPI, um hook de baixo nivel
    rodando em integridade media recebe ZERO eventos enquanto uma janela elevada
    tem o foco. O app nao trava e nao loga nada - o Win+A simplesmente para de
    responder ali, e vaza para a Central de Acoes do Windows.
    Criar a tarefa exige uma passada com UAC (docs/ARCHITECTURE.md secoes 3 e 6).

.PARAMETER Task
    Cria a Tarefa Agendada elevada em vez da entrada do registro.

.PARAMETER Name
    Nome da entrada / da tarefa. Padrao: wisper.

.PARAMETER KeepRunKey
    Com -Task, mantem tambem a entrada do registro. Por padrao ela e removida,
    porque as duas juntas lancam dois processos no logon e o segundo morre no
    mutex sem dizer nada.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\autostart_install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\autostart_install.ps1 -Task
#>
[CmdletBinding()]
param(
    [switch] $Task,
    [string] $Name = "wisper",
    [switch] $KeepRunKey
)

$ErrorActionPreference = "Stop"

$RUN_KEY = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Write-Fail {
    param([string] $Msg)
    Write-Host ""
    Write-Host "ERRO: $Msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Test-Elevated {
    try {
        $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object System.Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Show-WhyElevated {
    Write-Host ""
    Write-Host "Por que a versao Tarefa Agendada existe:" -ForegroundColor White
    Write-Host "  Um hook WH_KEYBOARD_LL em integridade media recebe ZERO eventos enquanto"
    Write-Host "  uma janela ELEVADA esta em foco (UIPI). Nesse momento o Win+A para de"
    Write-Host "  funcionar sem erro nenhum e vaza para a Central de Acoes. Rodando o app"
    Write-Host "  elevado, o hook continua recebendo tudo. O preco: uma passada de UAC"
    Write-Host "  agora, na criacao da tarefa. Medido - docs/ARCHITECTURE.md secoes 3 e 6."
}

function Show-HfHomeWarning {
    <#
        O scripts\run.ps1 exporta HF_HOME antes de lancar; o logon nao tem onde
        fazer isso - nem a chave Run nem a Tarefa Agendada aceitam variavel de
        ambiente. E o wispr/config.py usa os.environ.setdefault, que por definicao
        perde para um HF_HOME que ja exista no ambiente do usuario. Resultado
        silencioso: no logon o modelo e procurado no cache global, nao acha, e
        baixa 1,6 GB de novo para fora de models\.
    #>
    param([string] $ModelsDir)
    $scopes = @()
    foreach ($scope in @("User", "Machine")) {
        try {
            $v = [Environment]::GetEnvironmentVariable("HF_HOME", $scope)
        } catch {
            $v = $null
        }
        if ([string]::IsNullOrWhiteSpace($v)) { continue }
        if ($v.TrimEnd('\') -ieq $ModelsDir.TrimEnd('\')) { continue }
        $scopes += ("{0} = {1}" -f $scope, $v)
    }
    if ($scopes.Count -eq 0) { return }

    Write-Host ""
    Write-Host "AVISO: existe um HF_HOME permanente no ambiente:" -ForegroundColor Yellow
    foreach ($s in $scopes) { Write-Host ("    {0}" -f $s) -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "No logon nao da para exportar variavel antes do processo, e o config.py" -ForegroundColor Yellow
    Write-Host "usa os.environ.setdefault: esse valor vence models\. O wisper vai procurar" -ForegroundColor Yellow
    Write-Host "o modelo no cache global, nao achar, e baixar 1,6 GB de novo - sem erro" -ForegroundColor Yellow
    Write-Host "nenhum, so uma primeira transcricao que demora eternidades." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Resolva de um destes jeitos:" -ForegroundColor White
    Write-Host ("    setx HF_HOME `"{0}`"      (aponta o cache global para ca)" -f $ModelsDir)
    Write-Host  "    setx HF_HOME `"`"           (remove; o config.py assume models\)"
    Write-Host ""
    Write-Host "Vale so para o autostart: pelo scripts\run.ps1 o HF_HOME ja vai certo." -ForegroundColor DarkGray
}

$Root      = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPyw   = Join-Path $Root ".venv\Scripts\pythonw.exe"
$MainPyw   = Join-Path $Root "main.pyw"
$ModelsDir = Join-Path $Root "models"
$SetupPs1  = Join-Path $Root "scripts\setup.ps1"
$ThisPs1   = Join-Path $Root "scripts\autostart_install.ps1"
$StopPs1   = Join-Path $Root "scripts\stop.ps1"

if (-not (Test-Path -LiteralPath $VenvPyw)) {
    Write-Fail ("pythonw.exe do .venv nao existe em $VenvPyw. Rode primeiro:`r`n" +
                "  powershell -ExecutionPolicy Bypass -File `"$SetupPs1`"")
}
if (-not (Test-Path -LiteralPath $MainPyw)) {
    Write-Host "AVISO: $MainPyw ainda nao existe." -ForegroundColor Yellow
    Write-Host "       A entrada vai ser criada mesmo assim, mas nada sobe no logon" -ForegroundColor Yellow
    Write-Host "       enquanto esse arquivo nao aparecer." -ForegroundColor Yellow
}

# Aspas em cada caminho: sem elas, um espaco em qualquer pasta do caminho quebra a
# entrada do Run em silencio - o Windows tenta executar so o primeiro pedaco.
$Command = '"{0}" "{1}"' -f $VenvPyw, $MainPyw

# ------------------------------------------------------------ Tarefa Agendada --

if ($Task) {
    if (-not (Test-Elevated)) {
        Write-Host ""
        Write-Host "Este modo precisa de elevacao." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Criar uma tarefa com privilegio mais alto a partir de um shell comum"
        Write-Host "falha com 'Acesso negado' - o Windows exige uma passada de UAC para"
        Write-Host "registrar a tarefa (a tarefa criada, essa sim, roda sozinha no logon)."
        Write-Host ""
        Write-Host "Abra um PowerShell COMO ADMINISTRADOR e rode:" -ForegroundColor White
        Write-Host ""
        Write-Host "    powershell -ExecutionPolicy Bypass -File `"$ThisPs1`" -Task" -ForegroundColor Green
        Write-Host ""
        Write-Host "Ou peca o UAC daqui mesmo:" -ForegroundColor White
        Write-Host ""
        Write-Host ("    Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy','Bypass','-File','{0}','-Task'" -f $ThisPs1) -ForegroundColor Green
        Show-WhyElevated
        Write-Host ""
        Write-Host "Se nao quiser elevar, a entrada de registro comum resolve 95% dos casos:" -ForegroundColor DarkGray
        Write-Host "    powershell -ExecutionPolicy Bypass -File `"$ThisPs1`"" -ForegroundColor DarkGray
        Write-Host ""
        exit 2
    }

    if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "O modulo ScheduledTasks nao esta disponivel neste PowerShell." -ForegroundColor Yellow
        Write-Host "Crie a tarefa na mao, num prompt elevado:" -ForegroundColor Yellow
        Write-Host ""
        Write-Host ("    schtasks /Create /TN {0} /TR ""\""{1}\"" \""{2}\"""" /SC ONLOGON /RL HIGHEST /IT /F" -f $Name, $VenvPyw, $MainPyw)
        Write-Host ""
        exit 1
    }

    $userId = "$env:USERDOMAIN\$env:USERNAME"

    try {
        # Equivale a  schtasks /SC ONLOGON /RL HIGHEST /IT  -  mas via cmdlets, porque
        # o PowerShell 5.1 nao escapa as aspas internas de um /TR passado como
        # argumento nativo e a tarefa nasce com o caminho truncado.
        $action = New-ScheduledTaskAction -Execute $VenvPyw `
                                          -Argument ('"{0}"' -f $MainPyw) `
                                          -WorkingDirectory $Root

        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        try {
            # Atraso de 15 s: no logon a bandeja do sistema ainda nao aceita icone e o
            # pystray perde o dele em silencio.
            $trigger.Delay = "PT15S"
        } catch {
            Write-Host "    (nao consegui aplicar o atraso de 15 s no gatilho; segue sem ele)" -ForegroundColor DarkGray
        }

        # LogonType Interactive = /IT: roda com o usuario logado e nao guarda senha.
        $principal = New-ScheduledTaskPrincipal -UserId $userId `
                                                -LogonType Interactive `
                                                -RunLevel Highest

        # ExecutionTimeLimit zero = PT0S = sem limite. O padrao de 3 dias mataria um
        # app de bandeja que fica ligado o tempo todo.
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                                 -DontStopIfGoingOnBatteries `
                                                 -StartWhenAvailable `
                                                 -ExecutionTimeLimit ([TimeSpan]::Zero) `
                                                 -MultipleInstances IgnoreNew

        Register-ScheduledTask -TaskName $Name `
                               -Action $action `
                               -Trigger $trigger `
                               -Principal $principal `
                               -Settings $settings `
                               -Description "wisper - ditado por voz local (Win+A). Elevada para o hook enxergar janelas elevadas." `
                               -Force | Out-Null
    } catch {
        Write-Fail ("falha ao registrar a tarefa: " + $_.Exception.Message)
    }

    Write-Host ""
    Write-Host "Tarefa Agendada criada." -ForegroundColor Green
    Write-Host ("  nome     : {0}" -f $Name)
    Write-Host ("  gatilho  : no logon de {0} (+15 s)" -f $userId)
    Write-Host  "  privilegio: mais alto (elevada)"
    Write-Host ("  comando  : {0}" -f $Command)

    if (-not $KeepRunKey) {
        $existing = Get-ItemProperty -Path $RUN_KEY -Name $Name -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Remove-ItemProperty -Path $RUN_KEY -Name $Name -ErrorAction SilentlyContinue
            Write-Host ""
            Write-Host "Entrada de registro '$Name' removida para nao subir duas instancias." -ForegroundColor DarkGray
            Write-Host "(Use -KeepRunKey se quiser manter as duas.)" -ForegroundColor DarkGray
        }
    }

    Show-HfHomeWarning -ModelsDir $ModelsDir
    Show-WhyElevated
    Write-Host ""
    Write-Host "Testar agora, sem reiniciar:" -ForegroundColor White
    # Pare a instancia atual antes: a tarefa sobe uma segunda, que bate no mutex e
    # morre calada - e parece que a tarefa nao funcionou.
    Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`"   (fecha a instancia atual)" -f $StopPs1)
    Write-Host ("    Start-ScheduledTask -TaskName {0}" -f $Name)
    Write-Host "Remover depois:" -ForegroundColor White
    Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}\scripts\autostart_uninstall.ps1`" -Task   (elevado)" -f $Root)
    Write-Host ""
    exit 0
}

# ------------------------------------------------------- entrada HKCU\...\Run --

try {
    if (-not (Test-Path -LiteralPath $RUN_KEY)) {
        New-Item -Path $RUN_KEY -Force | Out-Null
    }
    New-ItemProperty -Path $RUN_KEY -Name $Name -Value $Command -PropertyType String -Force | Out-Null
} catch {
    Write-Fail ("nao consegui gravar em $RUN_KEY : " + $_.Exception.Message)
}

$check = (Get-ItemProperty -Path $RUN_KEY -Name $Name -ErrorAction SilentlyContinue).$Name
if ($check -ne $Command) {
    Write-Fail "a entrada foi gravada mas voltou diferente do esperado: $check"
}

Write-Host ""
Write-Host "Autostart instalado." -ForegroundColor Green
Write-Host ("  chave  : {0}" -f $RUN_KEY)
Write-Host ("  valor  : {0}" -f $Name)
Write-Host ("  comando: {0}" -f $Command)
Write-Host ""
Write-Host "Nao precisou de elevacao, e nao pede na hora do logon." -ForegroundColor DarkGray
Show-HfHomeWarning -ModelsDir $ModelsDir
Show-WhyElevated
Write-Host ""
Write-Host "Se voce trabalha com janelas elevadas abertas (Gerenciador de Tarefas como"
Write-Host "admin, prompt de admin), prefira:" -ForegroundColor White
Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`" -Task   (num shell elevado)" -f $ThisPs1) -ForegroundColor Green
Write-Host ""
Write-Host "Para remover:" -ForegroundColor White
Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}\scripts\autostart_uninstall.ps1`"" -f $Root)
Write-Host ""

exit 0
