"""
Uji deteksi wajah langsung dari kamera (dipakai perintah `flask face-check`).

Tidak ada gambar yang disimpan. Yang dihitung hanya statistik: berapa frame
yang berisi wajah, berapa yang layak, dan berapa lama setiap proses.
"""
import time

import cv2

from recognition.detector import assess

GREEN = (60, 180, 75)
ORANGE = (0, 140, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _text(img, label, org, color=WHITE, scale=0.7):
    """Menulis teks bergaris tepi; posisi digeser agar tidak keluar dari gambar."""
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 4)
    x = max(4, min(int(org[0]), img.shape[1] - tw - 4))
    y = max(th + 4, min(int(org[1]), img.shape[0] - 6))
    org = (x, y)
    cv2.putText(img, label, org, cv2.FONT_HERSHEY_SIMPLEX, scale, BLACK, 4)
    cv2.putText(img, label, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)


def annotate(frame, faces, header_lines):
    """Menggambar kotak wajah, titik wajah, dan status kualitas pada salinan frame."""
    view = frame.copy()
    for det, quality in faces:
        x, y, w, h = det.box
        color = GREEN if quality.ok else ORANGE
        cv2.rectangle(view, (x, y), (x + w, y + h), color, 2)
        for px, py in det.landmarks:
            cv2.circle(view, (int(px), int(py)), 3, color, -1)
        label = "LAYAK" if quality.ok else quality.reasons[0]
        _text(view, label, (x, max(20, y - 10)), color)
        _text(view, f"{quality.metrics['size']:.0f}px tajam {quality.metrics['sharpness']:.0f}",
              (x, y + h + 24), color, 0.55)
    for i, line in enumerate(header_lines):
        _text(view, line, (12, 30 + i * 28))
    return view


def run(cam, detector, embedder, quality_settings, seconds=20.0, on_frame=None,
        clock=time.perf_counter):
    start = clock()
    frames = with_face = good = embeds = 0
    detect_ms = embed_ms = 0.0
    sizes, sharpness = [], []

    while clock() - start < seconds:
        frame = cam.read()
        if frame is None:
            continue
        frames += 1

        t0 = clock()
        detections = detector.detect(frame)
        detect_ms += (clock() - t0) * 1000

        faces = []
        for det in detections:
            aligned = embedder.align(frame, det)
            q = assess(frame, det, quality_settings, crop=aligned)
            faces.append((det, q))

        if faces:
            with_face += 1
            main_det, main_q = faces[0]  # wajah terbesar = paling dekat kamera
            sizes.append(main_q.metrics["size"])
            sharpness.append(main_q.metrics["sharpness"])
            if main_q.ok:
                good += 1
                t1 = clock()
                embedder.embed(frame, main_det)
                embed_ms += (clock() - t1) * 1000
                embeds += 1

        if on_frame is not None:
            elapsed = max(clock() - start, 1e-6)
            live = {
                "fps": frames / elapsed,
                "detect_ms": detect_ms / frames,
                "faces": faces,
            }
            if on_frame(frame, live) is False:
                break

    def avg(values):
        return round(sum(values) / len(values), 1) if values else 0.0

    elapsed = max(clock() - start, 1e-6)
    return {
        "seconds": round(elapsed, 1),
        "frames": frames,
        "fps": round(frames / elapsed, 1),
        "with_face": with_face,
        "good": good,
        "detect_ms": round(detect_ms / frames, 1) if frames else 0.0,
        "embed_ms": round(embed_ms / embeds, 1) if embeds else 0.0,
        "face_size": avg(sizes),
        "face_sharpness": avg(sharpness),
        "min_sharpness_seen": round(min(sharpness), 1) if sharpness else 0.0,
    }
