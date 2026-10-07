"""
Pengambilan sampel wajah untuk pendaftaran (FR-03, PRD 6.1).

Aturan pengambilan sampel:
- Hanya diambil jika TEPAT SATU wajah terlihat, agar tidak salah orang.
- Wajah harus lolos pemeriksaan kualitas (ukuran, ketajaman, cahaya, arah).
- Sampel diberi jeda waktu agar bervariasi (tidak 5 frame yang sama persis).
- Semua sampel harus konsisten berasal dari orang yang sama.
- Wajah tidak boleh sudah terdaftar atas nama karyawan lain.

Yang disimpan hanya embedding (angka), bukan foto. Tidak ada gambar ke disk.
"""
import time
from dataclasses import dataclass, field

import numpy as np

from recognition.detector import assess
from recognition.embedder import cosine_similarity

DEFAULT_SAMPLES = 5
MIN_SAMPLE_INTERVAL = 0.6         # detik antar sampel
START_DELAY = 3.0                 # hitung mundur agar operator sempat minggir
MIN_CONSISTENCY = 0.5             # kemiripan minimal antar-PASANGAN sampel (SEMENTARA)


class EnrollmentError(Exception):
    pass


@dataclass
class EnrollmentResult:
    embeddings: list = field(default_factory=list)
    consistency: list = field(default_factory=list)
    rejected: dict = field(default_factory=dict)
    seconds: float = 0.0


def _note(rejected, reason):
    rejected[reason] = rejected.get(reason, 0) + 1


def collect(cam, detector, embedder, quality, samples=DEFAULT_SAMPLES, timeout=60.0,
            min_interval=MIN_SAMPLE_INTERVAL, on_frame=None, clock=time.perf_counter,
            start_delay=START_DELAY):
    """Mengambil `samples` embedding berkualitas dari kamera.

    on_frame(frame, status) dipanggil tiap frame; status berisi daftar wajah,
    jumlah sampel terkumpul, dan pesan untuk ditampilkan. Kembalikan False
    untuk membatalkan.
    """
    start = clock()
    last_taken = -1e9
    result = EnrollmentResult()

    while len(result.embeddings) < samples:
        now = clock()
        if now - start > timeout:
            raise EnrollmentError(
                f"Waktu habis: hanya {len(result.embeddings)} dari {samples} sampel layak. "
                f"Alasan penolakan terbanyak: {_top_reasons(result.rejected)}"
            )
        frame = cam.read()
        if frame is None:
            continue

        detections = detector.detect(frame)
        faces = []
        message = "Hadap ke kamera"
        for det in detections:
            aligned = embedder.align(frame, det)
            faces.append((det, assess(frame, det, quality, crop=aligned), aligned))

        countdown = start_delay - (now - start)
        if countdown > 0:
            # Masa bersiap: operator minggir, orang yang didaftarkan maju.
            message = f"Mulai dalam {int(countdown) + 1}... hanya orang yang didaftarkan di depan kamera"
        elif len(faces) > 1:
            message = "Hanya satu orang di depan kamera"
            _note(result.rejected, message)
        elif len(faces) == 1:
            det, q, aligned = faces[0]
            if not q.ok:
                message = q.reasons[0]
                _note(result.rejected, message)
            elif now - last_taken < min_interval:
                message = "Tahan posisi..."
            else:
                result.embeddings.append(embedder.embed_aligned(aligned))
                last_taken = now
                message = f"Sampel {len(result.embeddings)}/{samples} diambil"

        if on_frame is not None:
            status = {
                "faces": [(d, q) for d, q, _ in faces],
                "taken": len(result.embeddings),
                "samples": samples,
                "message": message,
            }
            if on_frame(frame, status) is False:
                raise EnrollmentError("Pendaftaran dibatalkan.")

    result.seconds = round(clock() - start, 1)
    return result


def _top_reasons(rejected):
    if not rejected:
        return "tidak ada wajah terdeteksi"
    ranked = sorted(rejected.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return ", ".join(f"{reason} ({count}x)" for reason, count in ranked)


def check_consistency(embeddings, min_similarity=MIN_CONSISTENCY):
    """Memastikan semua sampel berasal dari orang yang sama.

    Setiap PASANGAN sampel harus mirip. Membandingkan dengan rata-rata saja
    tidak cukup: campuran sampel dua orang menghasilkan rata-rata "di tengah"
    yang tetap mirip dengan semua sampel, sehingga campuran lolos.

    Mengembalikan daftar kemiripan semua pasangan sampel.
    """
    sims = [
        round(cosine_similarity(embeddings[i], embeddings[j]), 3)
        for i in range(len(embeddings))
        for j in range(i + 1, len(embeddings))
    ]
    if sims and min(sims) < min_similarity:
        raise EnrollmentError(
            "Sampel tidak konsisten (kemungkinan lebih dari satu orang atau wajah "
            f"berubah drastis). Kemiripan antar-sampel terendah {min(sims)} < "
            f"{min_similarity}. Ulangi pendaftaran."
        )
    return sims


def find_conflict(embeddings, matcher, employee_id):
    """Mencari karyawan LAIN yang wajahnya cocok dengan sampel ini.

    Mengembalikan MatchResult jika ada konflik, None jika aman.
    """
    if matcher.template_count == 0:
        return None
    mean = np.mean(np.stack(embeddings), axis=0)
    result = matcher.match(mean, exclude=employee_id)
    if result.score >= matcher.threshold:
        best_id = max(
            ((emp, s) for emp, s in matcher.scores(mean).items() if emp != employee_id),
            key=lambda kv: kv[1],
        )[0]
        result.employee_id = best_id
        return result
    return None
