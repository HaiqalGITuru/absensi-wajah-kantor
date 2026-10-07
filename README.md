# Sistem Absensi Wajah — Diskominfo Kabupaten Tabalong

Absensi otomatis dengan pengenalan wajah: karyawan cukup berjalan melewati
kamera di pintu masuk; sistem mengenali wajah, memastikan bukan foto, lalu
mencatat jam masuk/pulang sesuai jadwal kerja.

## Fitur

- Pendaftaran wajah (5 sampel) dengan pemeriksaan kualitas, konsistensi, dan
  pencegahan data ganda; templat wajah terenkripsi AES-256-GCM.
- Kios: deteksi YuNet, pengenalan SFace, pelacakan lintas frame, anti-foto
  MiniFASNet + tantangan menoleh, pesan di layar kios.
- Aturan absensi: toleransi terlambat, pulang cepat, shift lintas tengah
  malam, pencegahan absensi ganda di aplikasi dan database.
- Admin: dashboard kehadiran, karyawan, jadwal per hari, koreksi dan
  pembatalan beraudit, laporan dan ekspor CSV.
- Operasional: backup/pemulihan teruji, uji performa, daftar periksa keamanan.

## Teknologi

Python 3.11, Flask, MySQL/MariaDB (XAMPP), OpenCV 4 (`cv2.dnn`), Waitress.
Model: YuNet (MIT), SFace (Apache-2.0), MiniFASNet (Apache-2.0). Alasan
pemilihan dan lisensi: [docs/MODEL_DECISION.md](docs/MODEL_DECISION.md).

## Dokumentasi

| Dokumen | Isi |
|---|---|
| [docs/INSTALASI.md](docs/INSTALASI.md) | Memasang sistem di komputer kios |
| [docs/OPERASIONAL.md](docs/OPERASIONAL.md) | Rutinitas admin, karyawan baru, koreksi, penanganan gangguan |
| [docs/KEAMANAN.md](docs/KEAMANAN.md) | Daftar periksa sebelum dipakai resmi, penanganan insiden |
| [docs/BACKUP.md](docs/BACKUP.md) | Backup harian, uji pemulihan, pemulihan bencana, pengosongan data uji |
| [docs/MODEL_DECISION.md](docs/MODEL_DECISION.md) | Pemilihan model, lisensi, kalibrasi ambang dan anti-foto |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | Hasil uji performa |

## Mulai cepat (pengembangan)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # lalu isi
flask --app app db upgrade
flask --app app models-download
pytest
```
Untuk pemakaian resmi ikuti [docs/INSTALASI.md](docs/INSTALASI.md); jangan
memakai `flask run`.
