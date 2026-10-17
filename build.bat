@echo off
setlocal

REM ===========================================================================
REM  AutoClicker - Windows one-click build (PyInstaller, single-file)
REM
REM  Usage: double-click it, or run `build.bat` from a shell.
REM
REM  NOTE 1: this file must keep CRLF line endings. cmd.exe mis-parses
REM          multi-line blocks (if / for ... ) that use bare LF.
REM  NOTE 2: all console output is kept ASCII on purpose - the OEM code page
REM          on a Chinese Windows is GBK, and mixing encodings garbles it.
REM ===========================================================================

cd /d "%~dp0"

echo ============================================
echo  AutoClicker Build
echo ============================================

echo [1/4] Running smoke tests ...
REM errorlevel only reflects the LAST command, so each suite is checked
REM separately - otherwise a fast-failing smoke test hides behind this one.
python tests\test_smoke.py
if errorlevel 1 (
    echo.
    echo [ERROR] Smoke tests failed - not building a broken exe
    pause
    exit /b 1
)
python tests\test_probe.py
if errorlevel 1 (
    echo.
    echo [ERROR] Probe tests failed - not building a broken exe
    pause
    exit /b 1
)
echo.

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed for this python
    echo         Run: python -m pip install pyinstaller
    pause
    exit /b 1
)

if not exist main.py (
    echo [ERROR] main.py not found - run this script from the project root
    pause
    exit /b 1
)

REM Optional: put an AutoClicker.ico next to this script and it gets baked
REM into the exe. The window/tray icon is drawn in code either way, so this
REM only changes what Explorer shows for the file itself.
set "ICON_OPT="
if exist AutoClicker.ico set "ICON_OPT=--icon AutoClicker.ico"

echo [2/4] Cleaning build\ and dist\ ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/4] Building AutoClicker.exe ...
REM --onefile : single exe, no _internal folder
REM --windowed: no console window (this is a GUI app; it logs to a file)
REM excludes : drop the Qt subsystems this app never imports. PySide6 ships
REM            every module as its own DLL, so this is most of the size.
REM            Add --uac-admin here if the target program runs elevated
REM            (UIPI silently drops injected input otherwise).
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name AutoClicker ^
    %ICON_OPT% ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    --exclude-module numpy ^
    --exclude-module pandas ^
    --exclude-module scipy ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtWebChannel ^
    --exclude-module PySide6.QtWebSockets ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtCharts ^
    --exclude-module PySide6.QtDataVisualization ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.QtSql ^
    --exclude-module PySide6.QtTest ^
    --exclude-module PySide6.QtPdf ^
    --exclude-module PySide6.QtBluetooth ^
    --exclude-module PySide6.QtNfc ^
    --exclude-module PySide6.QtPositioning ^
    --exclude-module PySide6.QtSensors ^
    --exclude-module PySide6.QtSerialPort ^
    --exclude-module PySide6.QtSpatialAudio ^
    --exclude-module PySide6.QtStateMachine ^
    --exclude-module PySide6.QtRemoteObjects ^
    --exclude-module PySide6.QtNetworkAuth ^
    --exclude-module PySide6.QtDesigner ^
    --exclude-module PySide6.QtHelp ^
    --exclude-module PySide6.QtUiTools ^
    --exclude-module PySide6.QtSvgWidgets ^
    --exclude-module PySide6.QtTextToSpeech ^
    main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed
    pause
    exit /b 1
)

if not exist dist\AutoClicker.exe (
    echo.
    echo [ERROR] PyInstaller reported success but dist\AutoClicker.exe is missing
    pause
    exit /b 1
)

for %%I in ("dist\AutoClicker.exe") do set "EXE_SIZE=%%~zI"

echo.
echo [4/4] Build success
echo Output: dist\AutoClicker.exe  (%EXE_SIZE% bytes)
echo.
echo Reminder: profiles and logs live in %%LOCALAPPDATA%%\AutoClicker,
echo           so replacing the exe never loses user settings.
pause
