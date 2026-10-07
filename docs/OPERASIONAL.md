# Panduan operasional — untuk admin

## Rutinitas harian

**Pagi (sebelum jam masuk)**
1. Pastikan tiga jendela berjalan: *Server web*, *Layanan kamera*, dan halaman
   kios layar penuh. Jika komputer baru dinyalakan, ketiganya menyala otomatis.
2. Halaman kios menampilkan titik hijau **Kamera aktif**. Jika merah, lihat
   *Penanganan gangguan*.
3. Buka dashboard admin: tidak ada peringatan merah.

**Siang / setelah jam masuk**
- Dashboard → kotak **Belum masuk**: tindak lanjuti sesuai aturan kantor.
- Karyawan yang lupa/terlewat absen: menu **Absensi** → **Koreksi**
  (alasan wajib, tercatat di audit log).

**Sore**
- Backup berjalan otomatis (mis. 18:00). Komputer dan MySQL harus tetap
  menyala pada jam itu, atau ubah jam jadwal backup.

## Rutinitas berkala

| Kapan | Kegiatan | Cara |
|---|---|---|
| Mingguan | Salin backup terbaru ke penyimpanan di luar komputer | salin file `absensi_backup_*.zip` terbaru dari `BACKUP_DIR` |
| Mingguan | Tinjau penolakan anti-foto | Dashboard → Aktivitas terbaru, aksi `liveness_rejected` |
| Bulanan | Laporan kehadiran | menu **Laporan** → periode → Unduh rekap / detail (CSV) |
| Bulanan | Uji pemulihan backup | `flask --app app backup-test FILE --key FILE_KUNCI` |
| Bulanan | Pemeriksaan keamanan | `flask --app app security-check` |
| Setiap ada karyawan baru | Periksa kemiripan wajah | `flask --app app face-similarity-report` |

## Karyawan baru

1. Menu **Karyawan** → **Tambah karyawan** (kode = NIP). Jika muncul
   peringatan *nama sudah dipakai*, periksa apakah orangnya sama: jika sama,
   **aktifkan data lama**, jangan membuat data baru.
2. Hentikan sebentar *Layanan kamera* (tutup jendelanya), karena kamera hanya
   bisa dipakai satu program.
3. Daftarkan wajah:
   ```powershell
   flask --app app face-enroll --employee NIP
   ```
   Setelah Enter, operator **minggir**; karyawan berdiri sendirian 0,7–1 m di
   depan kamera selama hitung mundur, menghadap kamera, kepala digerakkan
   sedikit di antara sampel. Tanpa kacamata gelap/masker.
4. Jalankan `flask --app app face-similarity-report`. Pesan harus hijau
   (*Ambang cukup aman*).
5. Nyalakan lagi *Layanan kamera* (`jalankan_kios.bat`). Karyawan baru
   dikenali paling lambat 1 menit kemudian.

**Daftar ulang wajah** (penampilan berubah banyak, sering tidak dikenali):
```powershell
flask --app app face-enroll --employee NIP --replace "alasan daftar ulang"
```

## Karyawan keluar / pindah

1. Menu **Karyawan** → **Ubah** → **Cabut semua templat wajah** (alasan
   wajib). Data wajah tidak disimpan lebih lama dari yang diperlukan.
2. **Nonaktifkan** karyawan. Data dan riwayat absensinya tetap ada untuk
   laporan; karyawan tidak dihapus.

## Koreksi dan pembatalan absensi

- **Lupa absen / kamera gangguan**: Absensi → Koreksi → isi jam masuk/pulang.
  Status hadir/terlambat dihitung otomatis dari jadwal.
- **Kamera salah mengenali orang**: Absensi → Koreksi → **Batalkan absensi**
  pada orang yang tercatat keliru. Karyawan yang sebenarnya tetap bisa absen
  lewat kamera. Laporkan kejadian ini: jika berulang, periksa ambang dengan
  `face-similarity-report` dan daftar ulang wajah terkait.
- Tulis alasan yang jelas dan sopan: semua alasan tersimpan permanen di
  audit log beserta nama admin.

## Mengubah jadwal kerja

Menu **Jadwal kerja** → Ubah. Perubahan berlaku untuk absensi berikutnya;
catatan lama tetap dinilai dengan jadwal yang berlaku saat itu.

## Penanganan gangguan

| Gejala | Penyebab umum | Tindakan |
|---|---|---|
| Kios merah *Layanan kamera tidak berjalan* | Jendela *Layanan kamera* tertutup/galat | Jalankan `jalankan_kios.bat`; lihat pesan di jendelanya |
| Kios merah *Kamera bermasalah* | Kabel C270 lepas, kamera dipakai program lain (Zoom, Teams) | Colok ulang kabel, tutup program lain; layanan menyambung ulang otomatis |
| Kamera yang terbuka bukan C270 | `CAMERA_NAME` salah | `flask --app app camera-scan`, sesuaikan `CAMERA_NAME` |
| *Database tidak dapat dihubungi* | MySQL di XAMPP belum menyala | Start MySQL di XAMPP Control Panel |
| *Akses ke database ditolak* | Password di `.env` tidak sama dengan di phpMyAdmin | Samakan `DB_PASSWORD` |
| Karyawan tidak dikenali | Wajah belum terdaftar, karyawan nonaktif, pencahayaan buruk, wajah terlalu jauh/miring | Periksa status di menu Karyawan; perbaiki posisi/cahaya; daftar ulang wajah bila perlu |
| Diminta *Tolehkan kepala* | Anti-foto ragu (normal sesekali) | Ikuti instruksi: menoleh ke kiri/kanan **sesuai sisi tubuh karyawan** |
| Panel kuning *bukan wajah asli* untuk orang asli | Cahaya sangat silau/gelap, wajah terlalu dekat | Atur posisi dan cahaya; jika sering, catat dan laporkan untuk kalibrasi |
| Dua orang tertukar | Wajah sangat mirip | Batalkan absensi yang salah; jalankan `face-similarity-report`; daftar ulang wajah keduanya |
| Kios tidak bisa diklik/ditutup | Mode kios Edge | Tekan **Alt+F4** |
| Backup gagal | MySQL mati saat jadwal backup | Lihat `instance\backup_log.txt`; jalankan `backup-create` manual |
| `File backup tidak ditemukan` | Nama file salah ketik | Ketik awal nama lalu tekan **Tab** untuk melengkapi |

Selama kamera bermasalah, pakai **absensi alternatif** yang disepakati kantor
(mis. buku hadir), lalu admin memasukkan lewat menu Koreksi.

## Rotasi kunci biometrik

Jika ada dugaan file kunci bocor:
```powershell
flask --app app backup-create
flask --app app biometric-rotate-key
flask --app app backup-key E:\kunci-absensi
flask --app app backup-create
```
Simpan salinan kunci lama bersama backup lama yang masih membutuhkannya.

## Perintah penting

| Perintah | Fungsi |
|---|---|
| `security-check` | Daftar periksa keamanan |
| `camera-scan`, `camera-check` | Memeriksa kamera |
| `face-check`, `face-identify` | Uji deteksi dan pengenalan (tidak mencatat absensi) |
| `face-enroll`, `face-similarity-report` | Pendaftaran wajah dan pemeriksaan kemiripan |
| `attendance-simulate` | Simulasi aturan absensi (tidak menyimpan) |
| `biometric-status` | Memeriksa kunci dan templat |
| `backup-create`, `backup-verify`, `backup-test`, `backup-restore`, `backup-key` | Backup dan pemulihan (docs/BACKUP.md) |
| `perf-check` | Uji performa (database sementara) |
