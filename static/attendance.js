(() => {
  const byId = id => document.getElementById(id);
  const fileInput = byId("attendance-file");
  if (!fileInput) return;
  const state = { file: null, imageUrl: null, busy: false };
  const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;"," ":" "}[char] || "&#39;"));
  const eventName = () => byId("attendance-event").value.trim() || "Class Event";

  function setBusy(busy) {
    state.busy = busy;
    byId("attendance-generate").disabled = busy || !state.file;
    byId("attendance-generate").textContent = busy ? "Preparing report…" : "Generate report";
  }
  function showError(message = "") { byId("attendance-error").textContent = message; }
  function formData() { const data = new FormData(); data.append("file", state.file); data.append("event_name", eventName()); return data; }
  async function request(path) {
    const response = await fetch(path, { method: "POST", body: formData() });
    if (response.ok) return response;
    let message = `Could not generate the report (${response.status}).`;
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  function number(value) { return Number(value).toLocaleString("en-IN"); }
  function table(data, cumulative) {
    const headers = data.days.map((day, index) => `<th>Day ${index + 1}<br><small>${escapeHtml(day.slice(0, 5))}</small></th>`).join("");
    const cells = values => values.map(value => `<td>${number(value)}</td>`).join("");
    const rows = data.batches.map(batch => `<tr><td>${escapeHtml(batch.batch)}</td>${cells(cumulative ? batch.cumulative : batch.daily)}<td>${number(batch.total)}</td></tr>`).join("");
    const totals = cumulative ? data.total_cumulative : data.total_daily;
    const unique = cumulative ? "" : `<tr class="attendance-unique"><td>Unique students</td>${cells(data.unique_daily)}<td>${number(data.unique_overall)}</td></tr>`;
    return `<table><thead><tr><th>Batch</th>${headers}<th>Total</th></tr></thead><tbody>${rows}<tr class="attendance-total"><td>Grand total</td>${cells(totals)}<td>${number(data.grand_total)}</td></tr>${unique}</tbody></table>`;
  }
  function render(data) {
    byId("attendance-title").textContent = eventName();
    byId("attendance-period").textContent = `${data.days[0]} to ${data.days.at(-1)} · Event day ${data.days.length}`;
    byId("attendance-stats").innerHTML = [["Total scans", data.grand_total], ["Unique students", data.unique_overall], ["Batches", data.batches.length], ["Latest day scans", data.total_daily.at(-1)]].map(([label, value]) => `<div class="card"><p>${label}</p><strong>${number(value)}</strong></div>`).join("");
    const peak = Math.max(...data.total_daily, 1);
    byId("attendance-chart").innerHTML = data.total_daily.map((value, index) => `<div><b>${number(value)}</b><span style="height:${Math.max(3, value / peak * 108)}px"></span><small>${escapeHtml(data.days[index].slice(0, 5))}</small></div>`).join("");
    byId("attendance-daily").innerHTML = table(data, false);
    byId("attendance-cumulative").innerHTML = table(data, true);
    const notices = []; if (data.skipped_rows) notices.push(`${data.skipped_rows} row(s) were skipped because their scan date is invalid.`); if (data.missing_ids) notices.push(`${data.missing_ids} scan(s) have no student ID and are excluded from unique counts.`);
    const warning = byId("attendance-warning"); warning.textContent = notices.join(" "); warning.classList.toggle("hidden", !notices.length);
    byId("attendance-empty").classList.add("hidden"); byId("attendance-result").classList.remove("hidden");
  }
  async function previewImage() {
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
    state.imageUrl = URL.createObjectURL(await (await request("/api/attendance/image")).blob());
    byId("attendance-image").src = state.imageUrl;
  }
  async function generate() {
    if (!state.file || state.busy) return;
    setBusy(true); showError("");
    try { render(await (await request("/api/attendance/analyze")).json()); await previewImage(); }
    catch (error) { showError(error.message); }
    finally { setBusy(false); }
  }
  function pick(file) {
    if (!file || state.busy) return;
    showError(""); state.file = null;
    if (!/\.xlsx$/i.test(file.name)) return showError("Choose an .xlsx class-event export.");
    if (file.size > 20 * 1024 * 1024) return showError("The Excel file exceeds the 20 MB limit.");
    state.file = file; byId("attendance-filename").textContent = file.name;
    byId("attendance-event").value = file.name.replace(/\.xlsx$/i, "").replaceAll("_", " "); setBusy(false); generate();
  }
  async function download(path, extension) {
    if (!state.file || state.busy) return;
    setBusy(true); showError("");
    try {
      const blob = path === "/api/attendance/image" && state.imageUrl ? await fetch(state.imageUrl).then(response => response.blob()) : await (await request(path)).blob();
      const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `${eventName().replace(/[<>:"/\\|?*]/g, "_")}.${extension}`; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    } catch (error) { showError(error.message); } finally { setBusy(false); }
  }
  const drop = byId("attendance-drop");
  fileInput.addEventListener("change", event => pick(event.target.files[0]));
  drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("drag"); pick(event.dataTransfer.files[0]); });
  byId("attendance-generate").addEventListener("click", generate);
  byId("attendance-png").addEventListener("click", () => download("/api/attendance/image", "png"));
  byId("attendance-xlsx").addEventListener("click", () => download("/api/attendance/excel", "xlsx"));
  byId("attendance-print").addEventListener("click", () => window.print());
})();
