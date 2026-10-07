# Backup dan pemulihan (Tahap 5D)

## Apa yang dicadangkan

| Bagian | Perintah | Disimpan di |
|---|---|---|
| Seluruh database (karyawan, jadwal, absensi, templat wajah terenkripsi, audit log) | `backup-create` | `BACKUP_DIR` (mis. `D:\absensi-backup`), otomatis setiap hari |
| Kunci biometrik | `backup-key` | **Tempat terpisah**: flashdisk khusus kunci, disimpan terkunci |

**Keduanya dibutuhkan untuk memulihkan.** Tanpa kunci, templat wajah di backup
tidak bisa dibaca (semua karyawan harus mendaftar ulang). Kunci sengaja TIDAK
ikut di file backup: jika backup dan kunci disimpan bersama, siapa pun yang
mendapat folder itu bisa membuka data wajah pegawai. Perintah `backup-key`
menolak menyimpan kunci di folder backup atau di dalam folder proyek.

Setiap file backup berisi sidik jari SHA-256 tiap tabel; file yang diubah atau
rusak terdeteksi oleh `backup-verify`.

## Persiapan (sekali)

1. Atur folder backup di `.env`, sebaiknya di luar folder proyek:
   ```
   BACKUP_DIR=D:\absensi-backup
   BACKUP_KEEP=30
   ```
2. Salin kunci ke flashdisk khusus (misalnya drive `E:`):
   ```powershell
   flask --app app backup-key E:\kunci-absensi
   ```
   Simpan flashdisk ini di laci/lemari terkunci, terpisah dari salinan backup.
   **Ulangi setiap selesai `biometric-rotate-key`.**

## Backup harian otomatis (Windows Task Scheduler)

1. Buka **Task Scheduler** -> **Create Basic Task...**
2. Nama: `Backup Absensi Wajah`; Trigger: **Daily**, misalnya pukul 18:00
   (setelah jam pulang).
3. Action: **Start a program** -> Program:
   `D:\Folder_Coding\absensi-wajah-kantor\scripts\backup_harian.bat`
4. Selesai. Klik kanan tugas -> **Run** untuk mencoba, lalu periksa
   `instance\backup_log.txt` dan folder `BACKUP_DIR`.

MySQL (XAMPP) harus menyala saat jadwal backup berjalan. Backup lama
otomatis dihapus, menyisakan `BACKUP_KEEP` file terbaru.

**Mingguan:** salin file backup terbaru ke penyimpanan lain di luar komputer
kios (flashdisk/hard disk kantor yang BUKAN flashdisk kunci), agar backup tidak
ikut hilang jika komputer rusak.

## Uji pemulihan (bulanan)

Backup yang tidak pernah diuji belum tentu bisa dipakai. Sebulan sekali:
```powershell
flask --app app backup-test D:\absensi-backup\absensi_backup_YYYYMMDD_HHMMSS.zip --key E:\kunci-absensi\kunci_biometrik_....json
```
Perintah ini memulihkan backup ke database **sementara** (database asli tidak
disentuh), lalu memeriksa jumlah baris, dekripsi semua templat wajah, dan
pembuatan laporan. Hasil yang diharapkan: `Uji pemulihan BERHASIL`.

## Pemulihan saat bencana (komputer rusak / database hilang)

1. Siapkan komputer: Python, XAMPP, proyek dari GitHub, `.venv`, `.env`
   (sesuaikan `DB_*` dan `BIOMETRIC_KEY_FILE`), `models-download`.
2. Di phpMyAdmin buat database kosong `absensi_wajah` (utf8mb4) dan pastikan
   pengguna `absensi_app` punya hak akses ke database itu.
3. Buat struktur tabel: `flask --app app db upgrade`
4. Periksa dulu backup yang akan dipakai:
   ```powershell
   flask --app app backup-verify FILE_BACKUP --key FILE_KUNCI
   ```
5. Pulihkan (ketik `PULIHKAN` saat diminta):
   ```powershell
   flask --app app backup-restore FILE_BACKUP --key FILE_KUNCI
   ```
   Pemulihan hanya mau berjalan ke database KOSONG dengan revisi skema yang
   sama. Jika file kunci di `BIOMETRIC_KEY_FILE` belum ada, salinan kunci
   dipasang otomatis.
6. Periksa: `flask --app app biometric-status` (semua templat terbaca), buka
   dashboard dan menu Absensi.

## Pengosongan data uji sebelum dipakai resmi

Dilakukan SEKALI, setelah disetujui pembimbing, sebelum karyawan mulai absen
sungguhan. Data uji (absensi percobaan, koreksi uji, karyawan ganda) diarsipkan,
lalu database dimulai dari kosong.

1. **Arsipkan data uji:** `backup-create`, lalu `backup-test` file tersebut.
   Pindahkan file backup itu ke folder arsip, mis. `D:\absensi-arsip-uji`.
2. Hentikan `kiosk-run` dan server web.
3. phpMyAdmin -> database `absensi_wajah` -> **Operations** -> **Drop the
   database**. Lalu buat lagi database `absensi_wajah` (utf8mb4).
4. `flask --app app db upgrade`
5. **Kunci baru untuk data resmi:** pindahkan file kunci lama ke folder arsip
   yang sama dengan backup uji (hanya untuk membuka arsip itu), lalu
   `flask --app app biometric-keygen` dan `backup-key` ke flashdisk.
6. Lakukan daftar periksa keamanan (panduan operasional, Tahap 5E): ganti
   `SECRET_KEY` dan password database.
7. `create-admin`, buat jadwal kerja resmi, tambah karyawan dengan **NIP sebagai
   kode**, daftarkan wajah (`face-enroll`), lalu `face-similarity-report`.
8. `backup-create` pertama untuk data resmi.

## Cadangan tambahan

Ekspor phpMyAdmin (database -> **Export** -> SQL) tetap boleh dilakukan sebagai
cadangan tambahan, misalnya sebelum `flask db upgrade` besar. Ingat: templat
wajah di ekspor itu juga hanya bisa dibaca dengan kunci biometrik.
