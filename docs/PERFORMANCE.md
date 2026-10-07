# Uji performa (Tahap 5C)

Perintah: `flask --app app perf-check` (opsi: `--employees`, `--templates`,
`--days`, `--camera-seconds`).

Uji memakai **database SQLite sementara dan kunci enkripsi sementara** yang
dihapus setelah selesai. Database MySQL sungguhan tidak disambungkan (dibuktikan
oleh tes `test_uji_performa_tidak_menyentuh_database_utama` dan dengan
menjalankan uji saat alamat MySQL sengaja diisi alamat yang tidak ada).

## Sasaran

| Bagian | Sasaran |
|---|---|
| Pencocokan 1 wajah terhadap semua templat | < 5 ms |
| Muat + dekripsi semua templat (kios menyala / tiap menit) | < 2 detik |
| Halaman Absensi harian | < 1 detik |
| Dashboard | < 1 detik |
| Laporan 31 hari | < 3 detik |
| Ekspor CSV detail 31 hari | < 3 detik |
| Proses 1 frame kamera berwajah (deteksi + pengenalan + anti-foto) | < 150 ms |

## Hasil di lingkungan pengembangan (server uji, 7 Oktober 2026)

| Bagian | 150 karyawan × 5 templat, 31 hari | 500 karyawan × 5 templat, 62 hari |
|---|---|---|
| Pencocokan 1 wajah | 0,64 ms | 3,03 ms |
| Muat + dekripsi templat | 0,014 dtk (750 templat) | 0,052 dtk (2.500 templat) |
| Halaman Absensi harian | 0,016 dtk | 0,024 dtk |
| Dashboard | 0,021 dtk | 0,032 dtk |
| Laporan 31 hari | 0,171 dtk | 0,498 dtk |
| Ekspor CSV detail 31 hari | 0,181 dtk (361 KB) | 0,629 dtk (1,2 MB) |
| Proses 1 frame berwajah (model asli, gambar pengganti kamera) | 82 ms (p95 90 ms); frame tanpa wajah 25 ms | – |

## Hasil di komputer kantor (diisi setelah uji)

Perintah: `flask --app app perf-check --camera-seconds 30`

| Bagian | Hasil |
|---|---|
| Pencocokan 1 wajah | |
| Muat + dekripsi templat | |
| Halaman Absensi harian | |
| Dashboard | |
| Laporan 31 hari | |
| Ekspor CSV detail 31 hari | |
| Proses 1 frame berwajah (C270) | |

## Catatan

- SQLite dan MySQL berbeda; angka halaman web adalah perkiraan, tetapi untuk
  ukuran data kantor keduanya berada pada orde yang sama.
- Pada 500 karyawan, pencocokan naik ke ~3 ms, masih kecil dibanding waktu
  deteksi wajah per frame (~25–40 ms).
- Pemrosesan frame didominasi model (YuNet, SFace, dua model MiniFASNet), bukan
  jumlah karyawan terdaftar.
