@echo off
rem Windows 直接跑源码，开窗口界面（不用打包）
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 ( py -3 tracker_replace.py gui ) else ( python tracker_replace.py gui )
