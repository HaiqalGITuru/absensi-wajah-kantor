/*
 * Halaman kios: membaca status dari server setiap detik.
 * Waktu yang tampil adalah WAKTU SERVER (BR-06), bukan jam komputer ini.
 * Halaman ini tidak bisa mengubah data apa pun.
 */
(function () {
  "use strict";

  var root = document.querySelector(".kiosk");
  var url = root.dataset.statusUrl;
  var el = {
    clock: document.getElementById("clock"),
    date: document.getElementById("date"),
    panel: document.getElementById("panel"),
    message: document.getElementById("message"),
    hint: document.getElementById("hint"),
    dot: document.getElementById("camera-dot"),
    cameraText: document.getElementById("camera-text")
  };

  var IDLE_TEXT = "Silakan berjalan di depan kamera.";
  var OFFLINE_TEXT = "Layanan kamera tidak berjalan. Gunakan absensi alternatif atau hubungi admin.";
  var CAMERA_TEXT = {
    ok: "Kamera aktif",
    starting: "Kamera sedang dinyalakan…",
    error: "Kamera bermasalah — absensi tidak dapat dicatat",
    stopped: "Layanan kamera dihentikan",
    offline: "Layanan kamera tidak berjalan"
  };

  function setPanel(kind, text) {
    el.panel.className = "panel panel-" + kind;
    if (el.message.textContent !== text) {
      el.message.textContent = text;
    }
  }

  function render(data) {
    el.clock.textContent = data.time;
    el.date.textContent = data.date;

    var camera = data.online ? data.camera : "offline";
    el.dot.className = "dot " + (camera === "ok" ? "dot-ok" : camera === "starting" ? "dot-wait" : "dot-off");
    el.cameraText.textContent = CAMERA_TEXT[camera] || camera;

    if (!data.online || camera === "stopped") {
      setPanel("error", OFFLINE_TEXT);
    } else if (camera === "error") {
      setPanel("error", "Kamera bermasalah. Absensi belum dapat dicatat. Hubungi admin.");
    } else if (data.message) {
      setPanel(data.message.kind, data.message.text);
    } else {
      setPanel("idle", IDLE_TEXT);
    }

    var showHint = data.online && !data.message && data.hint;
    el.hint.hidden = !showHint;
    if (showHint) {
      el.hint.textContent = data.hint;
    }
  }

  function poll() {
    fetch(url, { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(render)
      .catch(function () {
        el.dot.className = "dot dot-off";
        el.cameraText.textContent = "Server tidak dapat dihubungi";
        setPanel("error", "Server absensi tidak dapat dihubungi. Hubungi admin.");
        el.hint.hidden = true;
      })
      .finally(function () { setTimeout(poll, 1000); });
  }

  poll();
})();
