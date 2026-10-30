/* Copyright Uploader isolated manual video source selector. No uploader engine controls. */
(() => {
  "use strict";
  function install() {
    const mode = document.getElementById("copyright-source-mode");
    if (!mode) return;
    const selector = document.getElementById("copyright-folder-file");
    const status = document.getElementById("copyright-folder-summary");
    const refresh = document.getElementById("copyright-folder-refresh");
    const sections = {
      drive_link: document.getElementById("copyright-drive-source"),
      pc_upload: document.getElementById("copyright-pc-source"),
      folder: document.getElementById("copyright-folder-source")
    };
    const sourceInput = {
      drive_link: document.querySelector('#copyright-drive-source input[name="drive_url"]'),
      pc_upload: document.querySelector('#copyright-pc-source input[name="video_file"]'),
      folder: selector
    };
    const selectedFromQuery = new URLSearchParams(location.search).get("source_mode");
    if (["folder", "pc_upload", "drive_link"].includes(selectedFromQuery)) mode.value = selectedFromQuery;
    function show() {
      const current = mode.value;
      for (const [kind, section] of Object.entries(sections)) {
        if (!section) continue;
        const visible = kind === current;
        section.hidden = !visible;
        section.style.setProperty("display", visible ? "block" : "none", "important");
        const input = sourceInput[kind];
        if (input) {
          input.required = visible;
          input.disabled = !visible;
        }
      }
    }
    let requestSequence = 0;
    async function updateFolders() {
      if (mode.value !== "folder" || !selector) return;
      const requestID = ++requestSequence;
      const previous = selector.value;
      if (status) status.textContent = "Checking available videos…";
      try {
        const response = await fetch("/copyright-uploader/api/copyright/folder-videos", {
          credentials: "same-origin", cache: "no-store",
          headers: {Accept: "application/json"}
        });
        if (!response.ok) throw new Error("Unable to load video folder (" + response.status + ")");
        const payload = await response.json();
        if (requestID !== requestSequence || mode.value !== "folder") return;
        const items = Array.isArray(payload.files) ? payload.files : [];
        selector.replaceChildren(new Option("Select a video from VPS folder", ""));
        for (const filename of items) selector.add(new Option(filename, filename));
        if (items.includes(previous)) selector.value = previous;
        if (status) status.textContent = items.length
          ? items.length + " available video" + (items.length === 1 ? "" : "s") + ". Already queued videos are hidden."
          : "No available videos. Choose Add to Copyright Uploader Folder in Video Merger, then refresh.";
      } catch (error) {
        if (requestID === requestSequence && status)
          status.textContent = error.message + ". Retry using Refresh Videos.";
      }
    }
    mode.addEventListener("change", () => {
      show();
      if (mode.value === "folder") updateFolders();
    });
    if (refresh) refresh.addEventListener("click", updateFolders);
    window.addEventListener("pageshow", () => {
      show();
      if (mode.value === "folder") updateFolders();
    });
    show();
    if (mode.value === "folder") updateFolders();
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, {once:true});
  else install();
})();
