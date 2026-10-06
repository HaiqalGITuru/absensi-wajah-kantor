"""
Pencocokan identitas (PRD 7.3, FR-06).

Embedding wajah di depan kamera dibandingkan dengan semua templat karyawan
aktif. Satu karyawan boleh punya beberapa templat; skor karyawan adalah skor
templat terbaiknya.

Keputusan:
- MATCH     : skor terbaik >= ambang DAN unggul cukup jauh dari karyawan kedua.
- UNKNOWN   : skor terbaik di bawah ambang (wajah tidak terdaftar).
- AMBIGUOUS : dua karyawan berbeda sama-sama di atas ambang dengan selisih
              terlalu kecil. Ditolak, tidak boleh menghasilkan absensi.

Hasil pencocokan BUKAN keputusan absensi. Hasil ini masih harus stabil di
beberapa frame (pelacakan) dan divalidasi aturan absensi (Tahap 4).
"""
from dataclasses import dataclass

import numpy as np

MATCH = "match"
UNKNOWN = "unknown"
AMBIGUOUS = "ambiguous"
NO_TEMPLATES = "no_templates"

# Ambang awal dari kode contoh resmi OpenCV untuk SFace (kemiripan kosinus).
# SEMENTARA: wajib dikalibrasi dengan data kantor (PRD 13.2).
DEFAULT_THRESHOLD = 0.363
DEFAULT_MARGIN = 0.05


@dataclass
class MatchResult:
    status: str
    employee_id: int = None
    score: float = 0.0
    second_score: float = 0.0

    @property
    def is_match(self):
        return self.status == MATCH


class Matcher:
    def __init__(self, templates, threshold=DEFAULT_THRESHOLD, margin=DEFAULT_MARGIN):
        """templates: daftar (employee_id, embedding)."""
        self.threshold = float(threshold)
        self.margin = float(margin)
        if templates:
            ids = np.array([emp for emp, _ in templates], dtype=np.int64)
            mat = np.stack([np.asarray(e, dtype=np.float32) for _, e in templates])
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = mat / norms
            self._ids = ids
            self._unique_ids = np.unique(ids)
        else:
            self._matrix = None
            self._ids = self._unique_ids = np.array([], dtype=np.int64)

    @property
    def employee_count(self):
        return int(len(self._unique_ids))

    @property
    def template_count(self):
        return 0 if self._matrix is None else int(self._matrix.shape[0])

    def scores(self, embedding):
        """{employee_id: skor terbaik} untuk semua karyawan."""
        if self._matrix is None:
            return {}
        q = np.asarray(embedding, dtype=np.float32)
        q = q / (float(np.linalg.norm(q)) or 1.0)
        sims = self._matrix @ q
        return {int(emp): float(sims[self._ids == emp].max()) for emp in self._unique_ids}

    def match(self, embedding, exclude=None):
        per_employee = self.scores(embedding)
        if exclude is not None:
            per_employee.pop(exclude, None)
        if not per_employee:
            return MatchResult(status=NO_TEMPLATES)

        ranked = sorted(per_employee.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0

        if best < self.threshold:
            return MatchResult(UNKNOWN, None, best, second)
        if second >= self.threshold and best - second < self.margin:
            return MatchResult(AMBIGUOUS, None, best, second)
        return MatchResult(MATCH, best_id, best, second)
