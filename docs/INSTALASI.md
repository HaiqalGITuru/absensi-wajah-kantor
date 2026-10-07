# Panduan instalasi — Sistem Absensi Wajah Diskominfo Tabalong

Panduan memasang sistem di **komputer kios** (Windows 10/11) dari awal.
Perkiraan waktu: 1–2 jam, belum termasuk pendaftaran wajah karyawan.

## 1. Perangkat

| Komponen | Kebutuhan |
|---|---|
| Komputer | Windows 10/11 64-bit, CPU 4 inti, RAM 8 GB, SSD |
| Kamera | Logitech C270 HD (USB), dipasang setinggi wajah, ±0,7–1 m dari posisi karyawan |
| Pencahayaan | Cahaya merata dari depan wajah; hindari jendela terang tepat di belakang karyawan |
| Media terpisah | Flashdisk/hard disk eksternal khusus untuk salinan kunci biometrik |

## 2. Program yang dipasang

1. **Python 3.11** (64-bit) dari python.org. Centang *Add python.exe to PATH*.
2. **XAMPP** (cukup komponen MySQL/MariaDB dan phpMyAdmin).
3. **Git for Windows**.
4. **Microsoft Edge** (sudah ada di Windows) untuk tampilan kios.

## 3. Mengambil kode

```powershell
cd D:\
git clone https://github.com/HaiqalGITuru/absensi-wajah-kantor.git
cd absensi-wajah-kantor
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Jika PowerShell menolak menjalankan `Activate.ps1`, jalankan sekali:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 4. Database

1. XAMPP Control Panel → **Start** MySQL. Jalankan XAMPP Control Panel sebagai administrator, lalu centang **Svc** (jalankan sebagai
   service) agar MySQL otomatis menyala saat Windows menyala.
2. **Isi password root MySQL** (bawaan XAMPP kosong): phpMyAdmin → User
   accounts → `root` (localhost) → Change password.
3. Buat database: phpMyAdmin → **New** → nama `absensi_wajah`, collation
   `utf8mb4_unicode_ci`.
4. Buat pengguna khusus: User accounts → **Add user account** → nama
   `absensi_app`, host `localhost`, password acak minimal 16 karakter.
   Jangan beri hak global; lalu buka tab **Database** pengguna itu dan beri
   **ALL PRIVILEGES** hanya untuk `absensi_wajah`.

## 5. File .env

Salin `.env.example` menjadi `.env`, lalu isi. Yang wajib:

| Kunci | Isi |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | hasil `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DB_USER` / `DB_PASSWORD` | `absensi_app` dan password langkah 4 |
| `OFFICE_TIMEZONE` | `Asia/Makassar` |
| `BIOMETRIC_KEY_FILE` | di luar folder proyek, mis. `D:\absensi-secrets\biometric_keys.json` |
| `BACKUP_DIR` | di luar folder proyek, mis. `D:\absensi-backup` |
| `CAMERA_NAME` | `C270` (lihat langkah 7) |
| `FACE_MATCH_THRESHOLD` | `0.60` (hasil kalibrasi; lihat docs/MODEL_DECISION.md) |
| `LIVENESS_MODE` | `passive+challenge` |

`.env` berisi rahasia: tidak boleh dikirim ke siapa pun dan tidak ikut ke Git.

## 6. Menyiapkan aplikasi

```powershell
flask --app app db upgrade              # membuat tabel
flask --app app models-download         # model YuNet, SFace, MiniFASNet (±41 MB)
flask --app app models-check            # harus 4 baris OK
flask --app app biometric-keygen        # kunci enkripsi templat wajah (sekali)
flask --app app backup-key E:\kunci-absensi   # salin kunci ke media terpisah
flask --app app create-admin            # akun admin pertama
pytest                                  # semua tes harus lulus
```

## 7. Kamera

```powershell
flask --app app camera-scan             # daftar kamera beserta namanya
flask --app app camera-check --seconds 10
```
- Pastikan baris *Kamera:* menunjukkan C270, bukan kamera laptop/virtual.
- Jika `camera-check` menyarankan `CAMERA_FORMAT_STRATEGY=...`, tambahkan ke
  `.env` agar kamera terbuka lebih cepat.
- `flask --app app face-check` untuk memeriksa wajah terdeteksi dan
  kualitasnya cukup pada posisi kios.

## 8. Data awal

1. Login di `http://127.0.0.1:5000/login` (jalankan server dulu, langkah 9).
2. **Jadwal kerja**: buat jadwal resmi (nama jelas, jam masuk/pulang tiap hari,
   termasuk Jumat, toleransi terlambat).
3. **Karyawan**: tambahkan dengan **NIP sebagai kode karyawan**.
4. Daftarkan wajah tiap karyawan (lihat docs/OPERASIONAL.md bagian
   *Karyawan baru*), lalu `flask --app app face-similarity-report`.

## 9. Menjalankan sistem

| Skrip (folder `scripts`) | Fungsi |
|---|---|
| `jalankan_server.bat` | Server web produksi (Waitress) di `http://127.0.0.1:5000` |
| `jalankan_kios.bat` | Layanan kamera yang mencatat absensi; menyala ulang otomatis jika berhenti |
| `buka_kios.bat` | Halaman kios layar penuh di Edge (keluar: Alt+F4) |
| `backup_harian.bat` | Backup harian (dijadwalkan lewat Task Scheduler) |

**Jangan memakai `flask run` untuk pemakaian resmi**: itu server
pengembangan. Server hanya mendengarkan `127.0.0.1` sehingga halaman admin
tidak bisa dibuka dari komputer lain di jaringan.

### Menyala otomatis saat Windows menyala

1. Tekan **Win+R**, ketik `shell:startup`, Enter.
2. Buat **shortcut** ke `jalankan_server.bat`, `jalankan_kios.bat`, dan
   `buka_kios.bat` di folder tersebut.
3. Jadwalkan `backup_harian.bat` (docs/BACKUP.md).
4. Mulai ulang komputer dan pastikan ketiganya menyala.

## 10. Pemeriksaan akhir

```powershell
flask --app app security-check
```
Semua butir otomatis harus **OK**, lalu selesaikan daftar periksa manual
(docs/KEAMANAN.md). Setelah itu jalankan uji performa singkat:
`flask --app app perf-check --camera-seconds 30`.
