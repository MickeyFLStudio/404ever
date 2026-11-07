@echo off
rem Builds dist\TS404Panel (PyInstaller) and the Windows installer (Inno Setup 6).
rem Needs: Rust (cargo) for the two DLLs, pip install pyinstaller pillow, and Inno Setup 6.
cd /d "%~dp0"
rem no personal paths inside the DLLs (Rust embeds source paths for panic messages)
set RUSTFLAGS=--remap-path-prefix=%USERPROFILE%\.cargo=cargo --remap-path-prefix=%~dp0=404ever\
pushd host && cargo build --release || (popd & exit /b 1)
popd
call :fresh host\target\release\ts404_host.dll ts404_host.dll || exit /b 1
pushd midi && cargo build --release || (popd & exit /b 1)
popd
call :fresh midi\target\release\ts404_midi.dll ts404_midi.dll || exit /b 1
python brand\make_icon.py || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name TS404Panel --icon brand\app.ico ^
  --add-data "index.html;." --add-binary "ts404_host.dll;." --version-file version_info.txt server.py || exit /b 1
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /Q installer\TS404Panel.iss || exit /b 1
echo Done: installer\Output\TS404Panel-Setup-*.exe
exit /b 0

:fresh
rem copy %1 over %2; a DLL that FL Studio (or the running server) has loaded cannot be
rem overwritten, but it can be renamed away (*.old is ignored by git)
if exist %2 (
  del /q %2 2>nul
  if exist %2 move /y %2 %2.%RANDOM%.old >nul || exit /b 1
)
copy /y %1 %2 >nul || exit /b 1
exit /b 0
