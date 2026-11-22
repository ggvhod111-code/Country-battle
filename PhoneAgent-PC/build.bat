@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ===== Python is NOT required: it is downloaded and installed into the .python folder here =====
set PYVER=3.12.8
set PYDIR=%~dp0.python
set PY=%PYDIR%\python.exe

if not exist "%PY%" (
  echo [1/4] Downloading private Python %PYVER% ^(only for building, ~25 MB^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '%TEMP%\py_setup.exe'"
  if errorlevel 1 goto :fail_net
  echo Installing into %PYDIR% ^(no admin rights, system is not touched^)...
  "%TEMP%\py_setup.exe" /quiet InstallAllUsers=0 TargetDir="%PYDIR%" PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=1 Include_pip=1 Shortcuts=0 AssociateFiles=0
  del "%TEMP%\py_setup.exe" >nul 2>nul
  if not exist "%PY%" goto :fail_py
) else (
  echo [1/4] Private Python already here.
)

echo [2/4] Installing build libraries...
"%PY%" -m pip install --upgrade pip pyinstaller pyautogui pillow
if errorlevel 1 goto :fail_pip

echo [3/4] Building PhoneAgent.exe...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name PhoneAgent --hidden-import pyautogui gui.py
if errorlevel 1 goto :fail_build

echo [4/4] Downloading adb ^(platform-tools^) for phone mode...
if not exist "dist\platform-tools\adb.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile '%TEMP%\pt.zip'; Expand-Archive -Force '%TEMP%\pt.zip' 'dist'; Remove-Item '%TEMP%\pt.zip'"
  if errorlevel 1 echo adb download failed - phone mode needs platform-tools next to PhoneAgent.exe
)

echo.
echo ================================================================
echo  Done: dist\PhoneAgent.exe
echo  Copy the whole "dist" folder anywhere. Python is NOT needed to run it.
echo  You can now delete the .python and build folders.
echo ================================================================
pause
exit /b 0

:fail_net
echo Could not download Python. Check the internet connection and run build.bat again.
pause
exit /b 1
:fail_py
echo Python install failed. Run build.bat again.
pause
exit /b 1
:fail_pip
echo pip install failed.
pause
exit /b 1
:fail_build
echo Build failed.
pause
exit /b 1
