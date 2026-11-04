@echo off
cd /d "%~dp0"
rem 注意: このファイルは Shift_JIS で保存する。起動の中身は app\launcher.py に書く(ここには足さない)

if not exist ".venv\Scripts\python.exe" (
    echo [エラー] .venv が見つかりません。docs\引き継ぎガイド.md の「初回セットアップ」を実施してください。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m app.launcher
if errorlevel 1 pause
