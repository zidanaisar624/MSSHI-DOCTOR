"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {situations, decodeReport, interpretReport} = require("../docs/panduan/guide.js");

test("setiap gejala mengarah ke pemeriksaan yang dapat disalin", () => {
  for (const s of Object.values(situations)) {
    assert.match(s.command, /^\.\\msshi-doctor\.cmd --(local|receipts|online|troubleshoot|check-app-update)( |$)/);
    assert.doesNotMatch(s.command, /--apply/);
  }
});
test("menerima UTF-8 dan UTF-16 PowerShell", () => {
  const json = '{"x":"pemeriksaan"}';
  for (const buf of [Buffer.from(json),Buffer.from("\ufeff"+json),Buffer.from("\ufeff"+json, "utf16le")]) {
    assert.deepEqual(decodeReport(buf), {x:"pemeriksaan"});
  }
});
test("menerjemahkan status dan tidak menampilkan data profil", () => {
  const d = {schema_version:1, exit_code:2, profiles:[{name:"PRIVATE"}], five_statuses:[]};
  assert.match(interpretReport(d).join(" "), /perlu diperiksa/);
  assert.doesNotMatch(interpretReport(d).join(" "), /PRIVATE/);
});
test("cache offline tidak disajikan sebagai rilis terverifikasi", () => {
  const d = {schema_version:1, exit_code:2, connection_status:"OFFLINE_WITH_LOCAL_FALLBACK", summary:{}, files:[]};
  assert.match(interpretReport(d)[0], /belum terverifikasi/);
});
test("menolak format dan hitungan asing", () => {
  assert.throws(() => interpretReport({schema_version:1, exit_code:0, random:true}));
  const d = {schema_version:1, exit_code:2, connection_status:"ONLINE_OK", summary:{missing:1, corrupted:0, drifted:2}, files:[]};
  assert.match(interpretReport(d)[1], /Hilang: 1/);
  d.summary.missing = -1;
  assert.throws(() => interpretReport(d));
});
