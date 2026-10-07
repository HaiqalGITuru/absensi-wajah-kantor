# Keamanan dan daftar periksa sebelum dipakai resmi

## Pemeriksaan otomatis

```powershell
flask --app app security-check
```
Memeriksa: mode produksi, `SECRET_KEY` dan password database (bukan contoh,
bukan yang pernah terlihat di tangkapan layar pengembangan), pengguna database
bukan root, lokasi kunci biometrik di luar proyek, `.env` tidak tercatat di
Git, semua templat wajah terbaca, file model asli, anti-foto aktif, ambang
kecocokan terkalibrasi, backup terbaru (≤ 2 hari), akun admin, nama jadwal
kerja, dan karyawan aktif tanpa wajah. Butir **PERLU** wajib dibereskan
sebelum dipakai resmi; perintah keluar dengan kode 1 selama masih ada.

## Daftar periksa manual

- [ ] Password **root** MySQL di XAMPP sudah diisi (bawaan XAMPP kosong).
- [ ] phpMyAdmin hanya bisa dibuka dari komputer ini (XAMPP bawaan: *Require local*; jangan diubah).
- [ ] Akun Windows komputer kios berpassword; layar terkunci otomatis saat ditinggal.
- [ ] Disk terenkripsi BitLocker, atau komputer di ruangan terkunci.
- [ ] Salinan kunci biometrik di media terpisah yang disimpan terkunci (docs/BACKUP.md).
- [ ] Data uji sudah dikosongkan (docs/BACKUP.md, *Pengosongan data uji*).
- [ ] Pembimbing meninjau docs/MODEL_DECISION.md, termasuk catatan lisensi dataset WIDER FACE.
- [ ] Karyawan diberi tahu tentang pemrosesan data wajah, tujuannya, dan hak-haknya;
      dasar pemrosesan dan persetujuan dikonfirmasi dengan bagian hukum/kepegawaian
      instansi. Data biometrik termasuk data pribadi yang bersifat spesifik menurut
      UU No. 27 Tahun 2022 tentang Pelindungan Data Pribadi.
- [ ] Metode absensi alternatif disepakati untuk saat kamera gangguan.
- [ ] Kios terpasang di area yang terlihat petugas (tidak ada anti-foto yang sempurna).

## Rancangan keamanan yang sudah diterapkan

| Aspek | Penerapan |
|---|---|
| Data wajah | Hanya templat angka (embedding) yang disimpan, terenkripsi AES-256-GCM dan terikat ke karyawan, model, dan versi kunci. Foto/video tidak disimpan. |
| Kunci enkripsi | File terpisah di luar proyek, tidak di database, tidak di Git, tidak ikut di backup database. Bisa dirotasi. |
| Akses admin | Login dengan password ter-hash, perlindungan CSRF, header keamanan, sesi berakhir saat keluar. |
| Halaman kios | Tanpa login, hanya-baca, hanya menampilkan nama depan. |
| Server | Waitress, hanya mendengarkan 127.0.0.1 (tidak terbuka ke jaringan). |
| Jejak | Login, perubahan karyawan/jadwal, pendaftaran/pencabutan wajah, koreksi/pembatalan absensi, penolakan anti-foto, ekspor laporan, backup, dan pemulihan tercatat di audit log. |
| Ekspor CSV | Aman dari CSV injection; setiap unduhan tercatat. |
| Anti-foto | Model MiniFASNet + tantangan menoleh; wajah yang dinilai palsu tidak pernah menghasilkan absensi. |

## Jika terjadi insiden

| Kejadian | Tindakan segera |
|---|---|
| `.env` terkirim/terlihat orang lain | Ganti `SECRET_KEY` (semua admin otomatis keluar) dan password database; jalankan `security-check`. |
| File kunci biometrik bocor | `backup-create`, `biometric-rotate-key`, `backup-key` ke media baru, `backup-create`. |
| Komputer kios hilang/dicuri | Pulihkan di komputer lain (docs/BACKUP.md), ganti semua password dan `SECRET_KEY`, rotasi kunci biometrik setelah pemulihan. Laporkan sesuai prosedur instansi. |
| Admin lupa password | Admin lain membuat akun baru dengan `flask --app app create-admin` di komputer kios. |
| Absensi atas nama orang lain (salah kenal/kecurangan) | Batalkan lewat menu Koreksi dengan alasan; periksa audit log dan `face-similarity-report`. |
