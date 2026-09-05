@echo off
setlocal DisableDelayedExpansion
set "LIGHTTABLE_BUNDLE=%~dp0"
if not exist "%LIGHTTABLE_BUNDLE%Python\python.exe" set "LIGHTTABLE_BUNDLE=%~dp0..\"
if not exist "%LIGHTTABLE_BUNDLE%Python\python.exe" (
  echo LightTable's bundled Python runtime was not found. Reinstall LightTable. 1>&2
  exit /b 1
)
"%LIGHTTABLE_BUNDLE%Python\python.exe" -B -m lighttable_cli %*
exit /b %errorlevel%
