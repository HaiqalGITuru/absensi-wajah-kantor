@echo off
REM ===================================================================
REM Menjalankan server web Sistem Absensi Wajah (Waitress, mode produksi).
REM Hanya bisa dibuka dari komputer ini: http://127.0.0.1:5000
REM Jangan tutup jendela ini selama sistem dipakai.
REM ===================================================================
title Absensi Wajah - Server web
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
echo Server web berjalan di http://127.0.0.1:5000  (Ctrl+C untuk berhenti)
waitress-serve --listen=127.0.0.1:5000 --threads=8 --call app:create_app
