(() => {
  const $ = id => document.getElementById(id);
  const input = $("book-json-file");
  const drop = $("book-json-drop");
  const button = $("book-json-convert");
  let file = null;
  function status(message = "", error = false) { const node = $("book-json-status"); node.textContent = message; node.classList.toggle("converter-error", error); }
  function choose(selected) {
    file = null; button.disabled = true; status("");
    if (!selected) return;
    if (!/\.xlsx$/i.test(selected.name)) return status("Choose a valid .xlsx Excel file.", true);
    if (selected.size > 20 * 1024 * 1024) return status("The Excel file exceeds the 20 MB limit.", true);
    file = selected; $("book-json-filename").textContent = selected.name; button.disabled = false;
  }
  input.addEventListener("change", event => choose(event.target.files[0]));
  drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("drag"); choose(event.dataTransfer.files[0]); });
  button.addEventListener("click", async () => {
    if (!file) return;
    button.disabled = true; status("Converting Excel to JSON…");
    try {
      const form = new FormData(); form.append("file", file);
      const response = await fetch("/api/book-event-json/convert", { method: "POST", body: form });
      if (!response.ok) { const data = await response.json(); throw new Error(data.error || "Could not convert the Excel file."); }
      const link = document.createElement("a"); link.href = URL.createObjectURL(await response.blob()); link.download = file.name.replace(/\.xlsx$/i, ".json"); link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      status("JSON file downloaded successfully.");
    } catch (error) { status(error.message, true); } finally { button.disabled = false; }
  });
})();
