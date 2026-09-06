@echo off
setlocal DisableDelayedExpansion
set "LIGHTTABLE_BUNDLE=%~dp0"
if not exist "%LIGHTTABLE_BUNDLE%Python\python.exe" set "LIGHTTABLE_BUNDLE=%~dp0..\"
if not exist "%LIGHTTABLE_BUNDLE%Python\python.exe" (
  echo LightTable's bundled Python runtime was not found. Reinstall LightTable. 1>&2
  exit /b 1
)
rem The embedded runtime ignores PYTHON* variables, so the bytecode cache is
rem an interpreter option. It shares the desktop app's cache folder and keeps
rem the installation directory untouched for a clean uninstall.
set "LIGHTTABLE_BYTECODE=%LOCALAPPDATA%\LightTable\python-bytecode"
if not defined LOCALAPPDATA set "LIGHTTABLE_BYTECODE=%TEMP%\LightTable\python-bytecode"
"%LIGHTTABLE_BUNDLE%Python\python.exe" -X "pycache_prefix=%LIGHTTABLE_BYTECODE%" -m lighttable_cli %*
exit /b %errorlevel%
