# Keputusan Pemilihan Model AI (Tahap 3B)

**Status:** diterapkan, menunggu tinjauan pembimbing
**Tanggal:** 6 Oktober 2026
**Mengubah PRD:** bagian 2.2 dan 7.2 ("Deteksi wajah: YOLO")

## Keputusan

| Fungsi | Model | File | Lisensi |
|---|---|---|---|
| Deteksi wajah | YuNet (OpenCV Zoo) | `face_detection_yunet_2023mar.onnx` | MIT |
| Pengenalan identitas | SFace (OpenCV Zoo) | `face_recognition_sface_2021dec.onnx` | Apache-2.0 |

Keduanya dijalankan dengan OpenCV (`cv2.FaceDetectorYN`, `cv2.FaceRecognizerSF`)
yang sudah menjadi dependensi proyek. Tidak ada pustaka AI tambahan.

## Alasan

PRD 2.2 menyatakan pemilihan model final mempertimbangkan akurasi, kecepatan,
kompatibilitas, dan ketentuan lisensi. Hasil pemeriksaan lisensi:

1. **YOLO dari Ultralytics** berlisensi AGPL-3.0. Menurut Ultralytics, kode,
   model, dan model hasil pelatihan/fine-tuning mereka mewajibkan seluruh
   proyek dirilis sebagai AGPL-3.0 atau membeli Enterprise License.
   Sumber: https://www.ultralytics.com/license
2. **Model InsightFace (ArcFace, mis. buffalo_l)**: kodenya MIT, tetapi model
   siap pakainya hanya untuk riset non-komersial.
   Sumber: https://github.com/deepinsight/insightface
3. **YuNet** (MIT) dan **SFace** (Apache-2.0) dari OpenCV Zoo berlisensi
   longgar, ringan untuk CPU, dan saling melengkapi (YuNet memberi 5 titik
   wajah yang dibutuhkan SFace untuk meluruskan wajah).
   Sumber: https://github.com/opencv/opencv_zoo

## Catatan yang belum terselesaikan

- **Dataset pelatihan.** Hampir semua detektor wajah (termasuk YuNet dan model
  "YOLO face") dilatih dengan dataset WIDER FACE. Ada sumber pihak ketiga yang
  mencatat lisensi dataset tersebut sebagai CC-BY-NC-ND-4.0 (non-komersial),
  tetapi belum dikonfirmasi dari sumber resmi. Persoalan ini sama untuk semua
  pilihan dan perlu ditinjau oleh pihak yang berwenang di Diskominfo.
- **Akurasi** belum diuji pada wajah karyawan dan kondisi kantor sebenarnya.
  Ambang kualitas (`FACE_MIN_SHARPNESS`) dan ambang kecocokan masih sementara
  dan harus dikalibrasi sesuai PRD 13.2.

## Integritas file model

File model diverifikasi dengan SHA-256 yang sama dengan penunjuk Git LFS resmi
OpenCV Zoo. File yang tidak cocok ditolak (`recognition/model_files.py`).

| File | SHA-256 |
|---|---|
| face_detection_yunet_2023mar.onnx | 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 |
| face_recognition_sface_2021dec.onnx | 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 |

## Jika kelak ingin memakai YOLO

Lapisan deteksi memakai antarmuka `detect(frame) -> list[FaceDetection]`
(`recognition/detector.py`). Detektor lain dapat ditambahkan sebagai kelas baru
tanpa mengubah bagian sistem lain, asalkan menghasilkan kotak wajah dan 5 titik
wajah. Perhatikan bahwa mengganti detektor tidak mengubah embedding, tetapi
tetap harus diuji ulang akurasinya.

---

# Tambahan Tahap 4D: anti-foto (keaktifan wajah, FR-11)

**Status:** diterapkan dalam uji coba, menunggu kalibrasi di lokasi
**Tanggal:** 6 Oktober 2026

## Temuan sebelum anti-foto dipasang (TC-19)

Uji di laptop pengembang (kamera bawaan, 6 Oktober 2026 malam): foto wajah
karyawan di layar HP **berhasil dikenali** sebagai karyawan tersebut setelah
sekitar 8 detik (skor kecocokan 0,37–0,40, sedikit di atas ambang 0,363).
Kesimpulan: pengenalan wajah saja tidak dapat membedakan orang asli dan foto.

## Keputusan

Dua lapisan (`recognition/liveness.py`, `LIVENESS_MODE=passive+challenge`):

| Lapisan | Cara kerja | Komponen | Lisensi |
|---|---|---|---|
| A. Pasif (utama) | Model menilai tanda pemotretan ulang (layar, cetakan) | MiniFASNetV2 (skala 2.7) + MiniFASNetV1SE (skala 4.0) | Apache-2.0 |
| B. Aktif (cadangan) | Jika A ragu, kios meminta menoleh ke arah acak | 5 titik wajah YuNet (tanpa model tambahan) | — |

- Sumber arsitektur dan bobot: https://github.com/minivision-ai/Silent-Face-Anti-Spoofing
  (file LICENSE: Apache License 2.0).
- File ONNX: https://github.com/yakhyo/face-anti-spoofing (file LICENSE: Apache License 2.0).
- Model dijalankan dengan `cv2.dnn`; hasilnya identik dengan ONNX Runtime
  (selisih < 1e-5), sehingga tidak ada dependensi tambahan.
- Validasi praproses dengan contoh resmi: gambar asli P(asli)=1,00; gambar
  palsu P(asli)=0,075 dan 0,014.

| File | SHA-256 |
|---|---|
| MiniFASNetV2.onnx | b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907 |
| MiniFASNetV1SE.onnx | ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676 |

## Keterbatasan yang diketahui

- Menurut penyedia model, deteksi keaktifan RGB **bergantung pada model kamera
  dan kondisi lokasi**, dan terpengaruh jika wajah tidak menghadap lurus.
  Ambang `LIVENESS_REAL_THRESHOLD=0.7` dan `LIVENESS_SPOOF_THRESHOLD=0.3` masih
  SEMENTARA dan wajib dikalibrasi dengan C270 di lokasi pemasangan.
- Tantangan menoleh dapat dikalahkan oleh video yang kebetulan menoleh ke arah
  yang sama; arah acak mengurangi, tetapi tidak menghilangkan, risiko ini.
- Topeng 3D berkualitas tinggi tidak termasuk ruang lingkup uji.
- Pendaftaran wajah (`face-enroll`) tidak memakai anti-foto karena dilakukan
  di bawah pengawasan admin.
- **Tidak ada anti-foto yang sempurna.** Pengawasan kios dan absensi alternatif
  tetap diperlukan (PRD 9.3); setiap penolakan tercatat di audit log
  (`liveness_rejected`) untuk ditinjau.
