"""
Identifikasi langsung: deteksi -> kualitas -> embedding -> pencocokan -> pelacakan.

Dipakai perintah `flask face-identify` untuk menguji pengenalan. Modul ini TIDAK
mencatat absensi. Pencatatan absensi baru dibuat di Tahap 4 dan akan memakai
peristiwa "dipastikan" (TrackEvent) dari pelacak.
"""
import time

import cv2

from recognition.detector import assess
from recognition.face_check import _text
from recognition.matcher import AMBIGUOUS, MATCH, UNKNOWN
from tracking.face_tracker import CONFIRMED

COLORS = {
    CONFIRMED: (60, 180, 75),     # hijau
    "pending": (0, 215, 255),     # kuning
    UNKNOWN: (60, 60, 230),       # merah
    AMBIGUOUS: (0, 140, 255),     # oranye
    "quality": (180, 180, 180),   # abu-abu
}


def process_frame(frame, detector, embedder, matcher, quality, tracker, now):
    """Satu langkah pengenalan. Mengembalikan (jejak, peristiwa, hasil_cocok)."""
    observations, results = [], []
    for det in detector.detect(frame):
        aligned = embedder.align(frame, det)
        q = assess(frame, det, quality, crop=aligned)
        result = None
        if q.ok:
            result = matcher.match(embedder.embed_aligned(aligned))
            results.append(result)
        observations.append((det.box, result, "" if q.ok else q.reasons[0]))
    tracks, events = tracker.update(observations, now)
    return tracks, events, results


def label_for(track, people):
    state = track.state
    if state == CONFIRMED:
        code, name = people.get(track.confirmed_id, ("?", "?"))
        return f"{code} {name}", COLORS[CONFIRMED]
    if state == UNKNOWN:
        return "Tidak dikenal", COLORS[UNKNOWN]
    if state == AMBIGUOUS:
        return "Ragu-ragu", COLORS[AMBIGUOUS]
    if track.last_reason:
        return track.last_reason, COLORS["quality"]
    return "Memverifikasi...", COLORS["pending"]


def annotate(frame, tracks, people, header_lines):
    view = frame.copy()
    for track in tracks:
        x, y, w, h = track.box
        label, color = label_for(track, people)
        cv2.rectangle(view, (x, y), (x + w, y + h), color, 2)
        _text(view, label, (x, y - 10), color)
        if track.last_score:
            _text(view, f"skor {track.last_score:.2f}", (x, y + h + 24), color, 0.55)
    for i, line in enumerate(header_lines):
        _text(view, line, (12, 30 + i * 28))
    return view


def run(cam, detector, embedder, matcher, quality, tracker, seconds=60.0,
        on_frame=None, on_event=None, clock=time.perf_counter):
    start = clock()
    stats = {
        "frames": 0, "process_ms": 0.0, "events": [], "unknown_tracks": set(),
        "ambiguous_tracks": set(), "match_scores": [], "unknown_scores": [],
        "ambiguous_results": 0,
    }

    while clock() - start < seconds:
        frame = cam.read()
        if frame is None:
            continue
        t0 = clock()
        tracks, events, results = process_frame(
            frame, detector, embedder, matcher, quality, tracker, t0
        )
        stats["process_ms"] += (clock() - t0) * 1000
        stats["frames"] += 1

        for r in results:
            if r.status == MATCH:
                stats["match_scores"].append(r.score)
            elif r.status == UNKNOWN:
                stats["unknown_scores"].append(r.score)
            elif r.status == AMBIGUOUS:
                stats["ambiguous_results"] += 1
        for t in tracks:
            if t.state == UNKNOWN:
                stats["unknown_tracks"].add(t.id)
            elif t.state == AMBIGUOUS:
                stats["ambiguous_tracks"].add(t.id)
        for e in events:
            stats["events"].append(e)
            if on_event is not None:
                on_event(e)

        if on_frame is not None and on_frame(frame, tracks) is False:
            break

    frames = stats["frames"] or 1
    confirm_times = [e.seconds_to_confirm for e in stats["events"] if not e.in_cooldown]
    return {
        "seconds": round(clock() - start, 1),
        "frames": stats["frames"],
        "process_ms": round(stats["process_ms"] / frames, 1),
        "confirmations": len([e for e in stats["events"] if not e.in_cooldown]),
        "repeats_in_cooldown": len([e for e in stats["events"] if e.in_cooldown]),
        "confirmed_people": sorted({e.employee_id for e in stats["events"]}),
        "avg_seconds_to_confirm": round(sum(confirm_times) / len(confirm_times), 2) if confirm_times else None,
        "unknown_tracks": len(stats["unknown_tracks"]),
        "ambiguous_tracks": len(stats["ambiguous_tracks"]),
        "ambiguous_results": stats["ambiguous_results"],
        "min_match_score": round(min(stats["match_scores"]), 3) if stats["match_scores"] else None,
        "max_unknown_score": round(max(stats["unknown_scores"]), 3) if stats["unknown_scores"] else None,
    }
