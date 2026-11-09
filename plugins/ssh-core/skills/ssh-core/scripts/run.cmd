@echo off
rem ssh-core launcher for Windows (review A1).
rem
rem SKILL.md told every caller to run `python scripts/ssh_execute.py ...`. On a
rem DSH host there is no `python` on PATH -- the interpreter lives inside the
rem runtime payload ($DSH_PYTHON), so the very first command failed with
rem CommandNotFoundException and every later call had to repeat the full path.
rem
rem Usage:
rem   scripts\run.cmd ssh_execute.py lab-240 "uptime" --json
rem   scripts\run.cmd selfcheck.py
rem   scripts\run.cmd doctor.py lab-240
rem
rem Interpreter selection, in order:
rem   1. %SSH_CORE_PYTHON%           explicit override
rem   2. %DSH_PYTHON%                the harness' own interpreter
rem   3. python / py -3 on PATH      a normal developer machine
rem   4. %DSH_HOME%\dsh-runtimes\*\dependencies\python\python.exe  (probe)
rem
rem Exit code: the script's own, unchanged. If no interpreter can be found the
rem launcher exits 3 (dependency_missing) with a one-line explanation, so this
rem path still honours the shared exit-code contract.
rem
rem Arguments are re-quoted one by one after `shift`: `%*` is *not* affected by
rem `shift` (it keeps meaning "everything the caller typed"), so using it after
rem shifting would pass the script name to the script itself.
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" goto no_target
shift

set "ARGS="
:collect
if "%~1"=="" goto resolve
set ARGS=%ARGS% "%~1"
shift
goto collect

:resolve
set "PY="
if defined SSH_CORE_PYTHON if exist "%SSH_CORE_PYTHON%" set "PY=%SSH_CORE_PYTHON%"
if not defined PY if defined DSH_PYTHON if exist "%DSH_PYTHON%" set "PY=%DSH_PYTHON%"
if not defined PY goto probe_path
goto run

:probe_path
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY where py >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=py -3"
if defined PY goto run
if not defined DSH_HOME goto no_python
for /d %%D in ("%DSH_HOME%\dsh-runtimes\*") do (
  if not defined PY if exist "%%D\dependencies\python\python.exe" set "PY=%%D\dependencies\python\python.exe"
)
if not defined PY goto no_python

:run
"%PY%" "%SCRIPT_DIR%%TARGET%" %ARGS%
exit /b %ERRORLEVEL%

:no_target
echo ssh-core: scripts\run.cmd needs a script name, e.g. run.cmd selfcheck.py 1>&2
exit /b 2

:no_python
echo ssh-core: no Python interpreter found. Set SSH_CORE_PYTHON (or DSH_PYTHON) to a python.exe, or put python on PATH. 1>&2
exit /b 3
