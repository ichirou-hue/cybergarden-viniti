<#
.SYNOPSIS
    Запуск бэкенда ВИНИТИ одной командой на Windows.

.DESCRIPTION
    Скрипт сам находит корень репозитория по pyproject.toml и src\viniti\cli.py,
    выбирает интерпретатор Python, проверяет, что он видит установленный пакет
    viniti, проверяет паспорт схемы, создаёт каталог data и базу, печатает
    счётчики и запускает python -m viniti server в текущем окне. Сервер не
    уходит в фон: окно остаётся открытым, пока его не остановят через Ctrl+C.

    Порядок выбора Python:
      1. параметр -Python;
      2. переменная окружения VINITI_PYTHON;
      3. .venv\Scripts\python.exe рядом со скриптом;
      4. python из PATH.

    Машинно-зависимых путей в скрипте нет: чужое окружение выбирается только
    через -Python или VINITI_PYTHON.

.PARAMETER Port
    Порт HTTP-сервера. По умолчанию 8765.

.PARAMETER Data
    Путь к базе SQLite. По умолчанию data\viniti.sqlite3 рядом со скриптом.
    Относительный путь считается от корня репозитория.

.PARAMETER Python
    Явный путь к python.exe. Если файла нет, скрипт падает с ошибкой,
    а не подставляет другой интерпретатор. Тот же выбор даёт переменная
    окружения VINITI_PYTHON, если -Python не указан.

.PARAMETER Check
    Только предварительные проверки (python -m viniti passport --check,
    python -m viniti db --init, python -m viniti db --stats).
    Сервер не запускается. Удобно для CI и для проверки окружения.

.PARAMETER NoPause
    Не ждать Enter ни при ошибке, ни при выходе.

.PARAMETER PauseOnExit
    Ждать Enter после завершения. run_server.bat включает это при двойном клике,
    чтобы окно с текстом ошибки не закрылось.

.EXAMPLE
    .\run_server.ps1

.EXAMPLE
    .\run_server.ps1 -Port 8813 -Data data\test.sqlite3

.EXAMPLE
    .\run_server.ps1 -Check -NoPause
#>

$ErrorActionPreference = 'Stop'

# Консоль Windows по умолчанию не в UTF-8, а Python с -X utf8 печатает UTF-8.
# Без этих двух строк русские сообщения превращаются в мусор.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Хост без настоящей консоли (например, ISE) - не повод падать.
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$port = 8765
$dataPath = $null
$pythonPath = $null
$checkOnly = $false
$noPause = $false
$pauseOnExit = $false
$scriptArgs = @($args)

function Show-Usage {
    Write-Host @"
Запуск бэкенда ВИНИТИ.

Использование:
  run_server.bat [параметры]

Параметры:
  -Port <число>     порт HTTP-сервера, по умолчанию 8765
  -Data <путь>      файл базы SQLite, по умолчанию data\viniti.sqlite3
  -Python <путь>    явный python.exe; без него берётся VINITI_PYTHON,
                    затем .venv\Scripts\python.exe, затем python из PATH
  -Check            только проверки, без запуска сервера
  -NoPause          не ждать Enter при ошибке или выходе
  -PauseOnExit      ждать Enter после завершения
  -Help             эта справка

Примеры:
  run_server.bat
  run_server.bat -Port 8813
  run_server.bat -Data data\demo.sqlite3 -Check -NoPause
  run_server.bat -Python C:\путь\к\python.exe
"@
}

function Fail {
    param(
        [string]$Message,
        [int]$Code = 1
    )
    Write-Host ""
    Write-Host "[ОШИБКА] $Message" -ForegroundColor Red
    if ($pauseOnExit -and -not $noPause) {
        Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null
    }
    exit $Code
}

function Get-NextValue {
    param(
        [string]$Name,
        [int]$Index
    )
    if ($Index + 1 -ge $scriptArgs.Count) {
        Fail "параметр $Name требует значение" 2
    }
    return [string]$scriptArgs[$Index + 1]
}

for ($i = 0; $i -lt $scriptArgs.Count; $i++) {
    $raw = [string]$scriptArgs[$i]
    # Принимаем -Port, --port, /port, -NoPause, --no-pause: CMD-пользователи пишут по-разному.
    $name = ($raw.ToLowerInvariant() -replace '^[-/]+', '') -replace '[-_]', ''
    switch ($name) {
        'h'        { Show-Usage; exit 0 }
        'help'     { Show-Usage; exit 0 }
        '?'        { Show-Usage; exit 0 }
        'port' {
            $value = Get-NextValue $raw $i
            $parsed = 0
            if (-not [int]::TryParse($value, [ref]$parsed)) {
                Fail "порт должен быть числом, получено: '$value'" 2
            }
            if ($parsed -lt 1 -or $parsed -gt 65535) {
                Fail "порт вне диапазона 1..65535: $parsed" 2
            }
            $port = $parsed
            $i++
            break
        }
        'data' {
            $dataPath = Get-NextValue $raw $i
            $i++
            break
        }
        'python' {
            $pythonPath = Get-NextValue $raw $i
            $i++
            break
        }
        'check' {
            $checkOnly = $true
            break
        }
        'nopause' {
            $noPause = $true
            break
        }
        'pauseonexit' {
            $pauseOnExit = $true
            break
        }
        default {
            Fail "неизвестный параметр: '$raw'. Справка: run_server.bat --help" 2
        }
    }
}

if ($noPause) {
    $pauseOnExit = $false
}

# --- корень репозитория: каталог самого скрипта, а не текущий каталог ---
$root = $PSScriptRoot
if (-not $root) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $root) {
    Fail "не удалось определить каталог скрипта. Положи run_server.ps1 в корень репозитория." 2
}

# Проверяем пакетную раскладку, а не отдельные скрипты в корне: модули живут
# в src\viniti, а запускаются через python -m viniti <command>.
$hasProject = Test-Path -LiteralPath (Join-Path $root 'pyproject.toml') -PathType Leaf
$hasCli = Test-Path -LiteralPath (Join-Path $root 'src\viniti\cli.py') -PathType Leaf
if (-not $hasProject -or -not $hasCli) {
    Fail "не найден корень репозитория ($root): ожидаются pyproject.toml и src\viniti\cli.py. Положи run_server.ps1 в корень репозитория." 2
}

try {
    Set-Location -LiteralPath $root
} catch {
    Fail "не удалось перейти в каталог репозитория: $root" 2
}

# --- выбор Python ---
$explicit = $null
if ($pythonPath) {
    $explicit = $pythonPath
} elseif ($env:VINITI_PYTHON) {
    $explicit = $env:VINITI_PYTHON
}

$python = $null
if ($explicit) {
    if (-not (Test-Path -LiteralPath $explicit -PathType Leaf)) {
        Fail "указанный Python не найден: $explicit" 2
    }
    $python = (Resolve-Path -LiteralPath $explicit).Path
} else {
    $candidates = @(
        (Join-Path $root '.venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $python = (Resolve-Path -LiteralPath $candidate).Path
            break
        }
    }
    if (-not $python) {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) {
            $python = $command.Source
        }
    }
}

if (-not $python) {
    Fail @"
Python не найден. Проверено:
  $root\.venv\Scripts\python.exe
  python в PATH

Создайте окружение и поставьте проект:
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e .

Если окружение лежит в другом месте, укажите его явно:
  .\run_server.bat -Python C:\путь\к\python.exe
или задайте переменную окружения VINITI_PYTHON.
"@ 2
}

Write-Host "Репозиторий: $root"
Write-Host "Python:     $python"

# --- выбранный Python должен запускаться, видеть пакет и его зависимости ---
# Родные команды пишут предупреждения в stderr, а при $ErrorActionPreference='Stop'
# PowerShell 5.1 превращает перенаправленный stderr в исключение. Поэтому пробу
# запускаем с временно ослабленным режимом и смотрим только код выхода.
function Test-PythonImports {
    param([string]$Code)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -X utf8 -c $Code 2>&1 | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return $code
}

& $python -X utf8 -c "import sys; print('Python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    Fail "выбранный Python не запускается: $python" 2
}

# Пакет должен быть установлен в это окружение, иначе python -m viniti ниже
# молча запустит другую копию или упадёт с ModuleNotFoundError. Ставить
# зависимости автоматически нельзя: это изменило бы окружение пользователя.
if ((Test-PythonImports "import viniti.cli") -ne 0) {
    Fail @"
выбранный Python не видит установленный пакет viniti:
  $python

Поставьте проект в это окружение в режиме разработки и повторите запуск:
  & '$python' -m pip install -e .

Если проект уже установлен в другое окружение, укажите его явно:
  .\run_server.bat -Python <путь\к\python.exe>
или задайте переменную окружения VINITI_PYTHON.
"@ 1
}

if ((Test-PythonImports "import pypdf, pdfplumber, pdfminer, reportlab, pymupdf") -ne 0) {
    Fail @"
в выбранном Python нет зависимостей сервера (pypdf, pdfplumber, pdfminer, reportlab, pymupdf).
Поставьте их вместе с проектом, без изменения других окружений:
  & '$python' -m pip install -e .

Если нужное окружение лежит в другом месте, укажите его через -Python
или переменную окружения VINITI_PYTHON.
"@ 1
}

# --- каталог базы ---
if (-not $dataPath) {
    $dataPath = Join-Path $root 'data\viniti.sqlite3'
} elseif (-not [System.IO.Path]::IsPathRooted($dataPath)) {
    $dataPath = Join-Path $root $dataPath
}
$dataPath = [System.IO.Path]::GetFullPath($dataPath)
$dataDir = Split-Path -Parent $dataPath
if (-not (Test-Path -LiteralPath $dataDir)) {
    try {
        New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    } catch {
        Fail "не удалось создать каталог для базы: $dataDir" 1
    }
}

Write-Host ""
Write-Host "== python -m viniti passport --check" -ForegroundColor Cyan
& $python -X utf8 -m viniti passport --check
$code = $LASTEXITCODE
if ($code -ne 0) {
    Fail @"
проверка паспорта не прошла (код $code).
passport.json расходится с валидаторами организаторов. Перегенерируйте его:
  & '$python' -X utf8 -m viniti passport
и повторите запуск.
"@ 1
}

Write-Host ""
Write-Host "== python -m viniti db --init" -ForegroundColor Cyan
& $python -X utf8 -m viniti db --init --db $dataPath
$code = $LASTEXITCODE
if ($code -ne 0) {
    Fail "не удалось создать схему базы (код $code): $dataPath" 1
}

Write-Host ""
Write-Host "== python -m viniti db --stats" -ForegroundColor Cyan
& $python -X utf8 -m viniti db --stats --db $dataPath
$code = $LASTEXITCODE
if ($code -ne 0) {
    Fail "не удалось прочитать базу (код $code): $dataPath" 1
}

if ($checkOnly) {
    Write-Host ""
    Write-Host "Проверки пройдены, сервер не запускался (-Check)." -ForegroundColor Green
    if ($pauseOnExit -and -not $noPause) {
        Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null
    }
    exit 0
}

Write-Host ""
Write-Host "== python -m viniti server --port $port --data $dataPath" -ForegroundColor Cyan
Write-Host "Остановка: Ctrl+C. Адрес сервер напечатает сам." -ForegroundColor DarkGray

& $python -X utf8 -m viniti server --port $port --data $dataPath
$serverCode = $LASTEXITCODE

Write-Host ""
if ($serverCode -eq 0) {
    Write-Host "Сервер остановлен." -ForegroundColor Green
} else {
    Write-Host "[ОШИБКА] python -m viniti server завершился с кодом $serverCode." -ForegroundColor Red
}

if ($pauseOnExit -and -not $noPause) {
    Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null
}
exit $serverCode
