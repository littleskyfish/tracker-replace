@echo off
rem 没有图形库/想用一个浏览器标签页当窗口时用这个（只监听 127.0.0.1）
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 ( py -3 tracker_replace.py ui ) else ( python tracker_replace.py ui )
