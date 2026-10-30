<#
.SYNOPSIS
Starts btlab when you sign in to Windows, or stops doing so with -Remove (SPEC.md section 23).

.DESCRIPTION
Registers the per-user logon task "btlab". The task runs a launcher written to %LOCALAPPDATA%\btlab\launch.ps1, on
the system drive. The launcher waits up to 10 minutes for the project folder, because the project lives on an
external drive, then starts .venv\Scripts\pythonw.exe -m btlab --config config.toml in that folder, and notes what
happened in %LOCALAPPDATA%\btlab\launch.log. If registering the task is refused, a shortcut that runs the launcher
goes in your Startup folder instead. Works in Windows PowerShell 5.1.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'btlab'
$LauncherDir = Join-Path $env:LOCALAPPDATA 'btlab'
$Launcher = Join-Path $LauncherDir 'launch.ps1'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'btlab.lnk'
$Project = Split-Path -Parent $PSScriptRoot
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$LauncherArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Launcher + '"'

# The launcher, with __PROJECT__ replaced by the project folder. pythonw.exe has no console, so the launcher notes in
# launch.log whether btlab started, and the exit code when it stopped at once.
$LauncherTemplate = @'
# Written by btlab's scripts\install-autostart.ps1: starts btlab once its project folder is there.
$project = '__PROJECT__'
$log = Join-Path $PSScriptRoot 'launch.log'

function Write-LaunchLog([string]$text) {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format 's') + ' ' + $text) -Encoding UTF8
}

# The external drive can mount after sign-in.
$deadline = (Get-Date).AddMinutes(10)
while (-not (Test-Path -LiteralPath $project -PathType Container)) {
    if ((Get-Date) -ge $deadline) {
        Write-LaunchLog "the project folder $project did not appear within 10 minutes; btlab was not started"
        exit 1
    }
    Start-Sleep -Seconds 5
}
$python = Join-Path $project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-LaunchLog "$python is missing; install the venv as README.md says"
    exit 1
}
$process = Start-Process -FilePath $python -ArgumentList '-m', 'btlab', '--config', 'config.toml' -WorkingDirectory $project -PassThru
# Holding the handle keeps the exit code readable once the process has ended.
$null = $process.Handle
if ($process.WaitForExit(15000)) {
    Write-LaunchLog "btlab stopped at once with exit code $($process.ExitCode) (1: see logs\btlab.log, 2: config.toml or taxes.toml, 3: btlab already runs); run.bat shows the message"
    exit 1
}
Write-LaunchLog "btlab started in $project, process $($process.Id)"
'@

function Install-Autostart {
    foreach ($needed in @('.venv\Scripts\pythonw.exe', 'config.toml')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Project $needed))) {
            throw "$needed is missing in the project folder $Project. Follow README.md's install steps first."
        }
    }
    New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
    $text = $LauncherTemplate.Replace('__PROJECT__', $Project.Replace("'", "''"))
    Set-Content -LiteralPath $Launcher -Value $text -Encoding UTF8
    try {
        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $action = New-ScheduledTaskAction -Execute $WindowsPowerShell -Argument $LauncherArguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        # The launcher exits once btlab runs; no time limit, so nothing stops it while it waits for the drive.
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    }
    catch {
        Install-StartupShortcut $_.Exception.Message
        return
    }
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut
    }
    Write-Host "Registered the logon task '$TaskName'. At each sign-in it runs $Launcher, which starts btlab in $Project."
    Write-Host "To start it now without signing out: Start-ScheduledTask -TaskName $TaskName"
}

function Install-StartupShortcut([string]$reason) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Shortcut)
    $link.TargetPath = $WindowsPowerShell
    $link.Arguments = $LauncherArguments
    $link.WindowStyle = 7
    $link.Description = 'Starts btlab'
    $link.Save()
    Write-Warning "Windows refused the logon task '$TaskName': $reason"
    Write-Host "Put a shortcut to the launcher in your Startup folder instead: $Shortcut"
    Write-Host "At each sign-in it runs $Launcher, which starts btlab in $Project."
}

function Remove-Autostart {
    $removed = @()
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        $removed += "the logon task '$TaskName'"
    }
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut
        $removed += "the Startup shortcut $Shortcut"
    }
    if (Test-Path -LiteralPath $LauncherDir) {
        Remove-Item -LiteralPath $LauncherDir -Recurse
        $removed += "the launcher folder $LauncherDir"
    }
    if ($removed.Count -eq 0) {
        Write-Host 'Nothing to remove: btlab does not start at sign-in.'
    }
    foreach ($item in $removed) {
        Write-Host "Removed $item."
    }
    Write-Host 'A btlab that runs now keeps running until you stop it.'
}

if ($Remove) {
    Remove-Autostart
}
else {
    Install-Autostart
}
