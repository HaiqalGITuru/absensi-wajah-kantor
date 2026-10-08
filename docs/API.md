# API — Sistem Absensi Wajah

API JSON untuk aplikasi lain (dashboard, panel admin Laravel, sistem
kepegawaian). Ada dua cakupan token:

- **read** — hanya membaca.
- **write** — membaca, menambah/mengubah/mengaktifkan/menonaktifkan karyawan
  dan jadwal, serta membaca audit log. Setara admin: simpan seaman password.

| Endpoint | Token | Isi |
|---|---|---|
| `GET /api/health` | tidak | Status layanan dan database |
| `GET /api/employees`, `GET /api/employees/{id}` | read | Karyawan |
| `GET /api/work-schedules`, `GET /api/work-schedules/{id}` | read | Jadwal kerja per hari |
| `GET /api/attendance`, `GET /api/attendance/{id}` | read | Catatan absensi |
| `GET /api/summary/today` | read | Ringkasan kehadiran satu hari (dashboard) |
| `GET /api/reports/summary`, `GET /api/reports/detail` | read | Rekap dan detail laporan (maks. 366 hari) |
| `POST /api/employees`, `PATCH /api/employees/{id}` | write | Tambah / ubah karyawan |
| `POST /api/employees/{id}/activate`, `/deactivate` | write | Aktifkan / nonaktifkan karyawan |
| `POST /api/work-schedules`, `PATCH /api/work-schedules/{id}` | write | Tambah / ubah jadwal |
| `POST /api/work-schedules/{id}/activate`, `/deactivate` | write | Aktifkan / nonaktifkan jadwal |
| `GET /api/audit-logs` | write | Audit log (`action`, `q`, `page`, `per_page`) |
| `POST /api/employees/{id}/face-enrollment` | write | Minta kamera kios mendaftarkan wajah (`replace_reason` untuk daftar ulang) |
| `GET /api/face-enrollments/{id}` | write | Kemajuan pendaftaran wajah |
| `POST /api/face-enrollments/{id}/cancel` | write | Batalkan pendaftaran wajah |

**Tidak ada endpoint hapus.** Karyawan dinonaktifkan, tidak dihapus.
Koreksi/pembatalan absensi tetap di admin sistem absensi (alasan wajib
tercatat).

**Tidak pernah dikirim lewat API:** templat/data wajah, skor kecocokan wajah,
password, audit log.

## Mendapatkan token

Token dibuat oleh admin di komputer server:
```powershell
flask --app app api-token-create --name "Andi - aplikasi rekap" --days 90
flask --app app api-token-create --name "Laravel - panel admin" --scope write   # maks. 90 hari, bawaan 30
```
- Token hanya ditampilkan **sekali**. Simpan di tempat aman (mis. variabel
  lingkungan / file `.env` aplikasi Anda), **jangan** ditulis di kode yang
  di-commit ke Git, dikirim lewat grup chat, atau ikut di tangkapan layar.
- Masa berlaku 1–365 hari (bawaan 90). Setelah kedaluwarsa minta token baru.
- Admin melihat daftar token dengan `api-token-list` dan mencabut dengan
  `api-token-revoke ID --reason "..."`. Pembuatan dan pencabutan tercatat
  di audit log.

Setiap permintaan menyertakan:
```
Authorization: Bearer aw_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Endpoint

### `GET /api/health`
```json
{"status": "ok", "database": "ok", "api_version": "1", "time": "2026-10-08T07:50:40+08:00"}
```
HTTP 503 dengan `"status": "error"` jika database tidak terhubung.

### `GET /api/employees`

| Parameter | Isi | Bawaan |
|---|---|---|
| `status` | `active`, `inactive`, `all` | `active` |
| `q` | cari kode atau nama | – |
| `department` | nama departemen persis | – |
| `page`, `per_page` | halaman; `per_page` 1–200 | 1, 50 |

```json
{
  "data": [
    {
      "id": 1,
      "employee_code": "2310010164",
      "full_name": "Haiqal Ramadhan",
      "department": "APTIKA",
      "is_active": true,
      "work_schedule": {"id": 1, "name": "Jam Kerja Kantor"},
      "face_registered": true
    }
  ],
  "page": 1, "per_page": 50, "total": 1, "pages": 1
}
```

### `GET /api/attendance`

| Parameter | Isi | Bawaan |
|---|---|---|
| `date` | satu tanggal `YYYY-MM-DD` | hari ini |
| `start`, `end` | rentang tanggal, maksimal 31 hari (jangan digabung dengan `date`) | – |
| `employee_id` / `employee_code` | satu karyawan | – |
| `department` | nama departemen persis | – |
| `status` | `hadir`, `terlambat`, `dibatalkan` | semua |
| `q` | cari kode atau nama karyawan | – |
| `page`, `per_page` | halaman; `per_page` 1–200 | 1, 50 |

Urutan: tanggal terbaru dulu, lalu nama. Hanya karyawan yang **punya catatan**
pada tanggal itu yang muncul; karyawan yang belum masuk tidak punya catatan
(bandingkan dengan `/api/employees` jika perlu daftar "belum masuk").

```json
{
  "data": [
    {
      "id": 2,
      "work_date": "2026-10-05",
      "employee": {"id": 1, "employee_code": "2310010164",
                   "full_name": "Haiqal Ramadhan", "department": "APTIKA"},
      "status": "terlambat",
      "check_in_at": "2026-10-05T07:48:00+08:00",
      "check_out_at": null,
      "late_minutes": 3,
      "left_early": false,
      "scheduled_start": "2026-10-05T07:30:00+08:00",
      "scheduled_end": "2026-10-05T16:00:00+08:00",
      "check_in_method": "kamera",
      "check_out_method": null
    }
  ],
  "start": "2026-10-01", "end": "2026-10-08",
  "page": 1, "per_page": 50, "total": 1, "pages": 1
}
```
Keterangan:
- `status`: `hadir`, `terlambat`, atau `dibatalkan` (dibatalkan admin; jam
  masuk/pulang kosong).
- `check_in_method` / `check_out_method`: `kamera` atau `admin` (hasil koreksi).
- `work_date` adalah tanggal **mulai** shift; jam pulang shift malam bisa
  jatuh di tanggal berikutnya.
- Semua waktu memakai zona waktu kantor (WITA, `+08:00`).

### `GET /api/attendance/{id}`
Satu objek dengan bentuk yang sama seperti satu elemen `data` di atas.

## Mengubah data (token write)

Body berupa JSON (`Content-Type: application/json`). Perubahan memakai layanan
yang SAMA dengan admin sistem absensi: validasi, aturan (kode unik, peringatan
nama ganda, jadwal yang masih dipakai tidak bisa dinonaktifkan) dan audit log
sama persis.

Kirim header **`X-Actor: <username admin aplikasi Anda>`**. Audit log mencatat
`[via API '<nama token>' (aw_xxxx…), pelaku: <X-Actor>]`. X-Actor hanya
keterangan; yang menentukan izin adalah token.

**Karyawan**
```json
POST /api/employees
{"employee_code": "199001012020011001", "full_name": "Siti Aminah",
 "department": "Sekretariat", "work_schedule_id": 1}
```
`PATCH` cukup mengirim field yang berubah. Jika nama sama dengan karyawan lain,
API menjawab 422 dengan `"needs_confirmation": true`; kirim ulang dengan
`"confirm_duplicate_name": true` jika memang orang berbeda.

**Jadwal kerja** (jam per hari; `weekday` 0 = Senin … 6 = Minggu)
```json
POST /api/work-schedules
{"name": "Jam Kerja Kantor", "late_tolerance_minutes": 15,
 "days": [{"weekday": 0, "start_time": "07:30", "end_time": "16:00"},
          {"weekday": 4, "start_time": "07:30", "end_time": "11:30"}]}
```
`PATCH` tanpa `days` mempertahankan jam yang ada; dengan `days`, daftar hari
diganti seluruhnya.

**Galat validasi (422)**
```json
{"error": "validasi_gagal", "message": "Periksa kembali data yang dikirim.",
 "fields": {"employee_code": "Kode '0001' sudah dipakai karyawan lain."},
 "needs_confirmation": false}
```

## Pendaftaran wajah lewat kamera kios (token write)

Wajah selalu didaftarkan memakai **kamera kios**, bukan kamera aplikasi
pemanggil: foto tidak pernah meninggalkan komputer kios dan hasilnya sesuai
kalibrasi kamera kios. API hanya menaruh permintaan di antrean; layanan kamera
kios (`kiosk-run`) mengambilnya, menampilkan nama depan karyawan di layar kios,
mengambil 5 sampel, lalu menyimpan templat terenkripsi.

```json
POST /api/employees/7/face-enrollment
{}                                              // pendaftaran pertama
{"replace_reason": "Sering tidak dikenali"}     // daftar ulang (wajib alasan)
```
Jawaban `202`:
```json
{"id": 12, "status": "pending", "samples_taken": 0, "samples_total": 5,
 "message": "Menunggu layanan kamera kios...", "kiosk_online": true, "finished": false, ...}
```
Pantau dengan `GET /api/face-enrollments/12` (mis. tiap 1 detik) sampai
`finished` bernilai `true`. Status: `pending` → `running` → `done` | `failed` |
`cancelled`; `pending` menjadi `expired` jika kios tidak mengambilnya dalam 2
menit (mis. `kiosk-run` tidak berjalan; lihat `kiosk_online`).

Pemeriksaan sama dengan `face-enroll` di terminal: satu wajah, kualitas,
konsistensi sampel, wajah tidak mirip karyawan lain, **ditambah** anti-foto.
Hanya satu pendaftaran aktif dalam satu waktu (kamera kios hanya satu).
Wajah yang mirip karyawan lain ditolak; pengecualian hanya lewat terminal kios
(`face-enroll --allow-similar`).

## Galat

Semua galat berbentuk JSON: `{"error": "kode", "message": "penjelasan"}`.

| HTTP | `error` | Arti |
|---|---|---|
| 400 | `parameter_tidak_valid` | Parameter salah format atau di luar batas |
| 401 | `token_diperlukan` | Header `Authorization` tidak ada |
| 401 | `token_tidak_valid` | Token salah, dicabut, atau kedaluwarsa |
| 403 | `cakupan_tidak_cukup` | Token read dipakai untuk perubahan / audit log |
| 422 | `validasi_gagal` | Data ditolak aturan sistem (lihat `fields`) |
| 400 | `body_tidak_valid` | Body bukan JSON objek atau berisi field tak dikenal |
| 404 | `tidak_ditemukan` | Endpoint atau data tidak ada |
| 405 | `metode_tidak_diizinkan` | Metode tidak tersedia (mis. DELETE) |
| 429 | `terlalu_banyak_permintaan` | Melebihi batas per menit (bawaan 120, `API_RATE_LIMIT`); tunggu sesuai header `Retry-After` |
| 503 | – | `/api/health`: database tidak terhubung |

## Contoh pemakaian

**PowerShell**
```powershell
$h = @{ Authorization = "Bearer $env:ABSENSI_TOKEN" }
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/attendance?date=2026-10-08" -Headers $h
```

**curl**
```bash
curl -H "Authorization: Bearer $ABSENSI_TOKEN" "http://127.0.0.1:5000/api/employees?per_page=100"
```

**Python (requests)**
```python
import os
import requests

BASE = "http://127.0.0.1:5000/api"
HEADERS = {"Authorization": f"Bearer {os.environ['ABSENSI_TOKEN']}"}

def semua_absensi(start, end):
    page, hasil = 1, []
    while True:
        r = requests.get(f"{BASE}/attendance", headers=HEADERS, timeout=10,
                         params={"start": start, "end": end, "page": page, "per_page": 200})
        r.raise_for_status()
        body = r.json()
        hasil.extend(body["data"])
        if page >= body["pages"]:
            return hasil
        page += 1
```

**JavaScript (fetch)**
```javascript
const res = await fetch("http://127.0.0.1:5000/api/employees", {
  headers: { Authorization: `Bearer ${process.env.ABSENSI_TOKEN}` },
});
if (!res.ok) throw new Error((await res.json()).message);
const { data } = await res.json();
```

## Akses dari komputer lain

Secara bawaan server hanya bisa diakses dari komputer kios itu sendiri
(`127.0.0.1`), demi keamanan. Pilihan untuk pengembang aplikasi lain:

1. **Disarankan untuk pengembangan:** jalankan salinan proyek ini di laptop
   sendiri dengan database uji sendiri (docs/INSTALASI.md, cukup langkah 3–6
   tanpa kamera), lalu buat token di salinan itu. Data pegawai sungguhan
   tidak keluar dari komputer kios.
2. **Akses ke data sungguhan lewat jaringan kantor** adalah keputusan terpisah
   yang perlu disetujui pembimbing/pengelola jaringan, karena tanpa HTTPS
   token dan data pegawai berpindah tanpa enkripsi. Syarat minimal: HTTPS
   (mis. reverse proxy dengan sertifikat), firewall yang hanya mengizinkan
   alamat IP aplikasi pemakai, dan token terpisah per aplikasi. Mengubah
   `--listen` di `scripts/jalankan_server.bat` saja **tidak cukup** dan akan
   ikut membuka halaman admin ke jaringan.
