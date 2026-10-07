@echo off
REM ===================================================================
REM Backup harian Sistem Absensi Wajah - Diskominfo Tabalong
REM Dijalankan oleh Windows Task Scheduler (lihat docs\BACKUP.md).
REM Hasil dan pesan galat dicatat di instance\backup_log.txt
REM ===================================================================
cd /d "%~dp0.."
if not exist instance mkdir instance
echo ==== %date% %time% ==== >> instance\backup_log.txt
call .venv\Scripts\activate.bat
flask --app app backup-create >> instance\backup_log.txt 2>&1
if errorlevel 1 (
  echo BACKUP GAGAL - periksa MySQL dan pesan di atas >> instance\backup_log.txt
  exit /b 1
)
exit /b 0
