"""
Pelacakan wajah antarframe (PRD 7.4, FR-07).

Tujuan:
- Wajah yang sama di frame berurutan dianggap satu "jejak" (track).
- Identitas baru dinyatakan PASTI setelah cocok berulang kali di jejak itu,
  sehingga satu frame yang keliru tidak langsung menghasilkan keputusan.
- Satu jejak tidak boleh mencampur dua identitas berbeda: jika itu terjadi,
  jejak diulang dari awal.
- Setelah seseorang dipastikan, jejaknya tidak terus-menerus menghasilkan
  keputusan baru, dan ada jeda per karyawan (cooldown).

Jeda di sini BUKAN satu-satunya pencegah absensi ganda. Pencegahan utama ada
di layanan absensi dan batasan database (Tahap 4, BR-05).
"""
import itertools
from collections import Counter, deque
from dataclasses import dataclass

from recognition.matcher import AMBIGUOUS, MATCH, UNKNOWN

PENDING = "pending"
CONFIRMED = "confirmed"


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    id: int
    box: tuple
    first_seen: float
    last_seen: float
    votes: deque
    confirmed_id: int = None
    confirmed_at: float = None
    last_status: str = PENDING
    last_score: float = 0.0
    last_reason: str = ""

    @property
    def state(self):
        if self.confirmed_id is not None:
            return CONFIRMED
        if len(self.votes) == self.votes.maxlen:
            statuses = Counter(v[0] for v in self.votes)
            if statuses[UNKNOWN] == len(self.votes):
                return UNKNOWN
            if statuses[AMBIGUOUS] >= len(self.votes) // 2 + 1:
                return AMBIGUOUS
        return PENDING


@dataclass
class TrackEvent:
    track_id: int
    employee_id: int
    seconds_to_confirm: float
    in_cooldown: bool
    score: float = None  # skor kecocokan terakhir saat identitas dipastikan


@dataclass
class TrackerSettings:
    iou_threshold: float = 0.3
    max_missing_seconds: float = 1.0
    window: int = 5
    confirm_votes: int = 3
    cooldown_seconds: float = 30.0

    @classmethod
    def from_config(cls, config):
        return cls(
            window=int(config.get("TRACK_WINDOW", cls.window)),
            confirm_votes=int(config.get("TRACK_CONFIRM_VOTES", cls.confirm_votes)),
            cooldown_seconds=float(config.get("IDENTITY_COOLDOWN_SECONDS", cls.cooldown_seconds)),
        )


class FaceTracker:
    def __init__(self, settings=None):
        self.s = settings or TrackerSettings()
        self.tracks = []
        self._ids = itertools.count(1)
        self._last_confirmed = {}  # employee_id -> waktu terakhir dipastikan

    def _new_track(self, box, now):
        return Track(
            id=next(self._ids), box=box, first_seen=now, last_seen=now,
            votes=deque(maxlen=self.s.window),
        )

    def _associate(self, boxes):
        """Pasangkan kotak baru dengan jejak lama berdasarkan tumpang-tindih (IoU)."""
        pairs = sorted(
            ((iou(t.box, b), ti, bi) for ti, t in enumerate(self.tracks) for bi, b in enumerate(boxes)),
            reverse=True,
        )
        used_t, used_b, matches = set(), set(), {}
        for score, ti, bi in pairs:
            if score < self.s.iou_threshold or ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            matches[bi] = self.tracks[ti]
        return matches

    def update(self, observations, now):
        """observations: daftar (box, MatchResult atau None, alasan_kualitas).

        MatchResult None berarti wajah terdeteksi tetapi kualitasnya tidak
        layak, sehingga tidak ikut "memilih" identitas.
        Mengembalikan (daftar jejak sesuai urutan observasi, daftar TrackEvent).
        """
        self.tracks = [t for t in self.tracks if now - t.last_seen <= self.s.max_missing_seconds]
        matches = self._associate([o[0] for o in observations])
        events, result_tracks = [], []

        for bi, (box, result, reason) in enumerate(observations):
            track = matches.get(bi)
            if track is None:
                track = self._new_track(box, now)
                self.tracks.append(track)
            track.box, track.last_seen, track.last_reason = box, now, reason or ""

            if result is not None:
                vote = (result.status, result.employee_id)
                track.last_status, track.last_score = result.status, result.score

                if track.confirmed_id is not None:
                    # Identitas lain muncul di jejak yang sudah pasti: jangan dicampur.
                    track.votes.append(vote)
                    others = [v for v in track.votes
                              if v[0] == MATCH and v[1] != track.confirmed_id]
                    if len(others) >= 2:
                        replacement = self._new_track(box, now)
                        replacement.votes.extend(others)
                        self.tracks[self.tracks.index(track)] = replacement
                        track = replacement
                else:
                    track.votes.append(vote)
                    ids = {v[1] for v in track.votes if v[0] == MATCH}
                    if len(ids) > 1:
                        # Dua identitas berbeda dalam satu jejak: mulai ulang.
                        track.votes.clear()
                        track.votes.append(vote)
                    else:
                        count = sum(1 for v in track.votes if v[0] == MATCH)
                        if count >= self.s.confirm_votes:
                            emp = next(iter(ids))
                            last = self._last_confirmed.get(emp)
                            in_cooldown = last is not None and now - last < self.s.cooldown_seconds
                            track.confirmed_id, track.confirmed_at = emp, now
                            if not in_cooldown:
                                self._last_confirmed[emp] = now
                            events.append(TrackEvent(
                                track.id, emp, round(now - track.first_seen, 2), in_cooldown,
                                round(track.last_score, 3),
                            ))
            result_tracks.append(track)
        return result_tracks, events
