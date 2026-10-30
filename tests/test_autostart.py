"""scripts/install-autostart.ps1 and run.bat (SPEC.md §23, AC-28), checked without running either of them."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install-autostart.ps1"
RUN_BAT = REPO / "run.bat"
PROJECT = r"E:\00_CODING\btlab"


def script() -> str:
    # Windows PowerShell 5.1 reads a file without a byte-order mark in the ANSI code page: ASCII reads the same in both.
    return SCRIPT.read_bytes().decode("ascii")


def launcher() -> str:
    """The launcher the script writes, with the project folder filled in."""
    (template,) = re.findall(r"^\$LauncherTemplate = @'\n(.*?)\n'@$", script(), flags=re.M | re.S)
    return template.replace("__PROJECT__", PROJECT)


def function(name: str) -> str:
    """The body of one of the script's functions, up to the next unindented line."""
    (body,) = re.findall(rf"^function {name}\b.*?\n(.*?)^\}}", script(), flags=re.M | re.S)
    return body


def test_the_script_names_the_logon_task_btlab_for_the_current_user():
    text = script()
    assert "$TaskName = 'btlab'" in text
    install = function("Install-Autostart")
    assert "Register-ScheduledTask -TaskName $TaskName" in install
    assert "New-ScheduledTaskTrigger -AtLogOn -User $user" in install
    assert "-LogonType Interactive -RunLevel Limited" in install


def test_the_launcher_goes_under_localappdata_and_the_task_runs_it():
    text = script()
    assert "$LauncherDir = Join-Path $env:LOCALAPPDATA 'btlab'" in text
    assert "$Launcher = Join-Path $LauncherDir 'launch.ps1'" in text
    assert "-File \"' + $Launcher + '\"'" in text
    install = function("Install-Autostart")
    assert "Set-Content -LiteralPath $Launcher -Value $text -Encoding UTF8" in install
    assert "New-ScheduledTaskAction -Execute $WindowsPowerShell -Argument $LauncherArguments" in install


def test_the_launcher_waits_ten_minutes_for_the_project_folder_then_starts_btlab_there():
    text = launcher()
    assert f"$project = '{PROJECT}'" in text
    assert "$deadline = (Get-Date).AddMinutes(10)" in text
    assert "while (-not (Test-Path -LiteralPath $project -PathType Container))" in text
    assert "Start-Sleep -Seconds 5" in text
    assert "$python = Join-Path $project '.venv\\Scripts\\pythonw.exe'" in text
    assert ("Start-Process -FilePath $python -ArgumentList '-m', 'btlab', '--config', 'config.toml' "
            "-WorkingDirectory $project") in text
    # The wait, the start and an exit at once each leave a line in launch.log.
    assert text.count("Write-LaunchLog \"") == 4


def test_a_refused_task_falls_back_to_a_startup_shortcut_and_says_so():
    text = script()
    assert "$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'btlab.lnk'" in text
    install = function("Install-Autostart")
    assert re.search(r"try \{.*Register-ScheduledTask.*\}\s*catch \{\s*Install-StartupShortcut \$_\.Exception\.Message",
                     install, flags=re.S)
    shortcut = function("Install-StartupShortcut")
    assert "$link.Arguments = $LauncherArguments" in shortcut and "$link.Save()" in shortcut
    assert "Startup folder instead" in shortcut


def test_remove_deletes_the_task_the_shortcut_and_the_launcher():
    text = script()
    assert text.startswith("<#") and "param([switch]$Remove)" in text
    assert re.search(r"if \(\$Remove\) \{\s*Remove-Autostart\s*\}\s*else \{\s*Install-Autostart\s*\}\s*$", text)
    remove = function("Remove-Autostart")
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in remove
    assert "Remove-Item -LiteralPath $Shortcut" in remove
    assert "Remove-Item -LiteralPath $LauncherDir -Recurse" in remove


@pytest.mark.parametrize("source", [script, launcher])
def test_no_syntax_newer_than_windows_powershell_5_1(source):
    code = "\n".join(line for line in source().splitlines() if not line.lstrip().startswith("#"))
    for newer in ("&&", "||", "??", "?.", "?[", " ? ", "-Parallel", "$PSStyle", "-AdditionalChildPath"):
        assert newer not in code, newer
    # Join-Path takes one child path in 5.1.
    for call in re.findall(r"Join-Path ([^\n|]*)", code):
        assert len(re.findall(r"'[^']*'|\(.*?\)|\$[\w:.]+", call)) == 2, call


def windows_powershell() -> str | None:
    return shutil.which("powershell")


@pytest.mark.skipif(windows_powershell() is None, reason="Windows PowerShell is not installed here")
def test_windows_powershell_parses_the_script_and_its_launcher(tmp_path):
    """PowerShell's own parser, which reads the files without running them."""
    written = tmp_path / "launch.ps1"
    written.write_text(launcher(), encoding="utf-8-sig")
    for path in (SCRIPT, written):
        command = ("$errors = $null; $null = [System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{str(path).replace(chr(39), chr(39) * 2)}', [ref]$null, [ref]$errors); "
                   "$errors | ForEach-Object { $_.Message }; exit $errors.Count")
        done = subprocess.run([windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr


def test_run_bat_starts_the_venv_python_with_config_toml_from_its_folder():
    text = RUN_BAT.read_bytes().decode("ascii")
    lines = [line.strip() for line in text.splitlines()]
    assert lines[0] == "@echo off" and 'cd /d "%~dp0"' in lines
    assert '".venv\\Scripts\\python.exe" -m btlab --config config.toml' in lines
    assert "if errorlevel 1 pause" in lines
    # Labels and goto misbehave in cmd.exe with LF line endings.
    assert not any(line.startswith(":") or line.lower().startswith("goto") for line in lines)
