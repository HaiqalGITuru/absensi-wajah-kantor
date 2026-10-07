@echo off
REM Membuka halaman kios layar penuh di Microsoft Edge. Keluar: Alt+F4.
start "" msedge --kiosk "http://127.0.0.1:5000/kiosk/" --edge-kiosk-type=fullscreen --no-first-run
