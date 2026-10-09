@echo off
chcp 65001 >nul
REM KOI BT Windows 打包：需要 Python 3.10-3.13（64 位）
python -m pip install -r requirements.txt || goto :err
python -m PyInstaller --noconfirm --windowed --name "KOI BT" --icon koi.ico ^
  --add-data "static;static" --add-data "browser_extension;browser_extension" ^
  --collect-all libtorrent --collect-submodules webview --hidden-import pystray._win32 main.py || goto :err
echo.
echo 打包完成：dist\KOI BT\KOI BT.exe
goto :eof
:err
echo 打包失败
exit /b 1
