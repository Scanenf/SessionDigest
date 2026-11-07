@echo off
chcp 65001 >nul
setlocal
set SCRIPT_DIR=%~dp0
rem 按需修改输出目录；日志追加到 nightly.log
python "%SCRIPT_DIR%session_digest.py" --nightly --obsidian-dir "D:\vault\AI摘要" >> "%SCRIPT_DIR%nightly.log" 2>&1
endlocal
