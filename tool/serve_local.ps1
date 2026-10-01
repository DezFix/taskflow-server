# Запускает сервер TaskFlow отдельным процессом.
#
# Фоновой задачи в PowerShell недостаточно: она заканчивается вместе
# с сессией, не дождавшись старта сервера. Скрипт пишет лог
# построчно, чтобы по нему было видно, что сервер действительно
# поднялся и на каком порту.
#
# Запуск из корня репозитория:
#   powershell -ExecutionPolicy Bypass -File tool\serve_local.ps1
#
# Если окружение лежит вне репозитория, укажите интерпретатор явно:
#   powershell -File tool\serve_local.ps1 -Python C:\path\to\venv\Scripts\python.exe

param(
    [string]$Python,
    [string]$Host_,
    [string]$Port
)

$ErrorActionPreference = 'Continue'
$env:PYTHONPATH = ''
$env:PYTHONIOENCODING = 'utf-8'
$env:TASKFLOW_HOST = if ($Host_) { $Host_ } elseif ($env:TASKFLOW_HOST) { $env:TASKFLOW_HOST } else { '0.0.0.0' }
$env:TASKFLOW_PORT = if ($Port) { $Port } elseif ($env:TASKFLOW_PORT) { $env:TASKFLOW_PORT } else { '8080' }

# Корень выводим из расположения скрипта, иначе путь пришлось бы
# править на каждой машине.
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'server.log'
$ready = Join-Path $root 'server.ready'

# Интерпретатор: явно указанный, затем локальное окружение проекта,
# затем python из PATH.
if (-not $Python) {
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        $venvPython = Join-Path $root 'venv\Scripts\python.exe'
    }
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}

Set-Location $root
Remove-Item -Force $ready, $log -ErrorAction SilentlyContinue

& $Python -m app.cli serve *>&1 |
    ForEach-Object { "$([DateTime]::Now.ToString('HH:mm:ss')) $_" | Tee-Object -FilePath $log -Append }

# Отметка появляется только после остановки сервера.
"EXIT=$LASTEXITCODE" | Out-File -FilePath $ready -Encoding UTF8