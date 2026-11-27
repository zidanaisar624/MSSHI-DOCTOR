"use strict";

const situations = {
  start: {
    title: "Periksa instalasi lokal", command: ".\\msshi-doctor.cmd --local",
    detail: "Lihat lokasi instalasi, profil yang terdeteksi, dan status Doctor di laptop.",
    after: "Cocokkan lokasi target dengan instalasi yang ingin diperiksa. Jika salah, tentukan folder lewat --target-root."
  },
  startup: {
    title: "Telusuri gangguan saat membuka Hermes", command: ".\\msshi-doctor.cmd --troubleshoot",
    detail: "Baca hasil pemeriksaan tampilan, gateway, folder skill, Lightcraft, dan perintah MSSHI.",
    after: "Jika Hermes menyatakan sedang memperbarui aplikasi, tunggu pembaruan selesai sebelum mencoba membuka ulang."
  },
  files: {
    title: "Bandingkan dengan MSSHI v3.2.1", command: ".\\msshi-doctor.cmd --online --ref v3.2.1",
    detail: "Doctor mengambil daftar file rilis resmi dan menunjukkan file yang hilang, rusak, atau berbeda.",
    after: "Periksa versi dan daftar file. Jika perbedaan memang perlu dipulihkan, pilih file tersebut di aplikasi lalu buka pratinjau."
  },
  undo: {
    title: "Temukan catatan perbaikan", command: ".\\msshi-doctor.cmd --receipts",
    detail: "Cari catatan dengan waktu dan target yang cocok dengan perbaikan yang ingin kamu batalkan.",
    after: "Buka catatan online-repair di panel pemulihan. Periksa konflik sebelum mengonfirmasi rollback."
  },
  app_update: {
    title: "Cek pembaruan aplikasi MSSHI Doctor", command: ".\\msshi-doctor.cmd --check-app-update",
    detail: "Memeriksa rilis terbaru khusus repositori MSSHI-DOCTOR tanpa mengubah file instalasi MSSHI lokal.",
    after: "Jika ada versi baru, tinjau changelog terlebih dahulu lalu jalankan --self-update --dry-run."
  }
};

function decodeReport(bytes) {
  const utf16le = bytes[0] === 255 && bytes[1] === 254;
  const utf16be = bytes[0] === 254 && bytes[1] === 255;
  const encoding = utf16le ? "utf-16le" : utf16be ? "utf-16be" : "utf-8";
  return JSON.parse(new TextDecoder(encoding, {fatal:true}).decode(bytes));
}

function interpretReport(data) {
  if (!data || data.schema_version !== 1 || ![0,1,2].includes(data.exit_code)) {
    throw new Error("Pilih laporan JSON dari perintah --local atau --online.");
  }
  const local = Array.isArray(data.five_statuses) && Array.isArray(data.profiles);
  const online = data.summary && Array.isArray(data.files) && typeof data.connection_status === "string";
  if (!local && !online) throw new Error("Laporan ini bukan hasil pemeriksaan lokal atau perbandingan online.");
  if (online && !["ONLINE_OK","ONLINE_ETAG_CACHED"].includes(data.connection_status)) {
    return ["Rilis GitHub belum terverifikasi", "Periksa koneksi atau batas permintaan GitHub, lalu ulangi perbandingan. Tunda perbaikan online."];
  }
  const headings = ["Pemeriksaan selesai", "Ada masalah inti", "Ada hal yang perlu diperiksa"];
  let detail = local ? "Profil yang terdeteksi: " + data.profiles.length + "." : "";
  if (online) {
    const keys = ["missing","corrupted","drifted"];
    if (!keys.every(k => Number.isSafeInteger(data.summary[k]) && data.summary[k] >= 0)) {
      throw new Error("Ringkasan jumlah file belum lengkap.");
    }
    detail = "Hilang: " + data.summary.missing + ". Rusak: " + data.summary.corrupted + ". Berbeda: " + data.summary.drifted + ".";
  }
  const next = ["Simpan hasil ini sebagai pembanding.", "Baca rincian masalah dan cocokkan lokasi instalasinya.", "Periksa peringatan sebelum memilih perbaikan."];
  return [headings[data.exit_code], detail + " " + next[data.exit_code]];
}

function enableGuide(doc) {
  const byId = id => doc.getElementById(id);
  function show(key) {
    const step = situations[key];
    byId("next-title").textContent = step.title;
    byId("cmd").textContent = step.command;
    byId("explanation").textContent = step.detail;
    byId("after").textContent = step.after;
    byId("copy").textContent = "Salin";
  }
  doc.querySelectorAll('input[name="case"]').forEach(input => input.addEventListener("change", () => show(input.value)));
  show(doc.querySelector('input[name="case"]:checked').value);
  byId("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(byId("cmd").textContent);
      byId("copy").textContent = "Tersalin";
    } catch {
      const range = doc.createRange();
      range.selectNodeContents(byId("cmd"));
      const selection = doc.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      byId("copy").textContent = "Tekan Ctrl+C";
    }
  });
  byId("report-file").addEventListener("change", async event => {
    const input = event.target;
    const file = input.files[0];
    const output = byId("report-result");
    output.replaceChildren();
    if (!file) return;
    try {
      if (file.size > 4 * 1024 * 1024) throw new Error("Ukuran laporan melebihi batas 4 MB. Buat laporan lokal yang lebih kecil.");
      const result = interpretReport(decodeReport(new Uint8Array(await file.arrayBuffer())));
      if (file !== input.files[0]) return;
      const heading = doc.createElement("h3");
      const paragraph = doc.createElement("p");
      [heading.textContent, paragraph.textContent] = result;
      output.replaceChildren(heading, paragraph);
    } catch (error) {
      if (file !== input.files[0]) return;
      output.textContent = error instanceof SyntaxError || error instanceof TypeError ? "File belum terbaca sebagai JSON Doctor. Buat ulang dengan perintah di atas." : error.message;
    }
  });
}

if (typeof document !== "undefined") enableGuide(document);
if (typeof module !== "undefined") module.exports = {situations, decodeReport, interpretReport};
