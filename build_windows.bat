@echo off
rem ============================================================
rem  在 Windows 上打单文件 tracker-replace.exe（pyinstaller 不能跨平台交叉编译，
rem  所以 exe 必须在 Windows 上打一次；打完全目录拷走直接用）
rem  需要：Python 3.8+（python.org 装的默认带 tkinter，无需额外东西）
rem ============================================================
cd /d "%~dp0"
set PY=py -3
where py >nul 2>nul || set PY=python

echo [1/3] 装打包器（工具本身只用标准库，不装第三方包）
%PY% -m pip install --upgrade pip pyinstaller || goto :err

echo [2/3] 打包（--noconsole：只出窗口，不弹黑框）
%PY% -m PyInstaller --onefile --clean --noupx --noconsole --name tracker-replace --hidden-import tracker_ui tracker_replace.py || goto :err

echo [3/3] 完成
echo   窗口界面   dist\tracker-replace.exe
echo   命令行     dist\tracker-replace.exe doctor --old old-tracker.example
echo   exe 在 dist\tracker-replace.exe，可以单独拷走
goto :eof

:err
echo 打包失败，看上面的报错（最常见是 pip 装 pyinstaller 卡住，挂上代理再跑一次）
pause
