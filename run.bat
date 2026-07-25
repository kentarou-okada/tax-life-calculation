@echo off
rem 家計・税金管理アプリ 起動バッチ（Windows）
rem ダブルクリックでセットアップ確認 → DB初期化(未作成時) → サーバ起動 → ブラウザを開く
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [初回セットアップ] 仮想環境を作成し依存をインストールします...
  py -3 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist "data\app.db" (
  echo [初期化] データベースを作成します...
  ".venv\Scripts\python.exe" -m scripts.init_db
)

echo.
echo サーバを起動します: http://127.0.0.1:8000/tax
echo 停止するにはこのウィンドウで Ctrl+C を押してください。
echo.
start "" "http://127.0.0.1:8000/"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

endlocal
