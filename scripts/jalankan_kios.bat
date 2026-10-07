@echo off
REM ===================================================================
REM Menjalankan layanan kamera kios (MENCATAT ABSENSI).
REM Jika berhenti karena galat, otomatis dijalankan ulang setelah 10 detik.
REM Tutup jendela ini untuk menghentikan layanan kamera.
REM ===================================================================
title Absensi Wajah - Layanan kamera
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
:ulang
flask --app app kiosk-run --no-preview
echo Layanan kamera berhenti. Dijalankan ulang dalam 10 detik (tutup jendela untuk batal)...
timeout /t 10
goto ulang
