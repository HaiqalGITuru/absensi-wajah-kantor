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
