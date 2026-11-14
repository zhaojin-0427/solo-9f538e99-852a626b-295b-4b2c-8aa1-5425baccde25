(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);

  const state = {
    preview: null,
    activeFilter: "all",
    busy: false,
  };

  const ACTION_LABELS = {
    add: "新增",
    replace: "替换",
    ignore: "忽略",
    conflict: "冲突",
  };

  const REASON_LABELS = {
    new: "该站该小时尚无已发布记录",
    newer_revision: "修订号高于已发布记录",
    superseded_in_batch: "同站同小时存在更高修订号（本批）",
    duplicate_in_batch: "同站同小时同修订号的重复行",
    already_published: "与已发布记录完全一致，无需重复发布",
    older_than_published: "修订号低于已发布记录，不予覆盖",
    revision_value_mismatch: "同修订号数值不一致，整批拒绝",
  };

  const fileInput = $("#file-input");
  const dropzone = $("#dropzone");
  const dropTitle = $("#drop-title");
  const previewBtn = $("#preview-btn");
  const uploadStatus = $("#upload-status");
  const confirmBtn = $("#confirm-btn");
  const confirmStatus = $("#confirm-status");

  function setStatus(node, text, busy) {
    node.textContent = text || "";
    node.classList.toggle("busy", Boolean(busy));
  }

  function formatDate(dateString) {
    if (!dateString) return "";
    const [year, month, day] = dateString.split("-");
    return `${year} 年 ${Number(month)} 月 ${Number(day)} 日`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  async function readJson(response) {
    const text = await response.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return { error: "invalid_response", message: text };
    }
  }

  function finishUploadState() {
    previewBtn.disabled = state.busy || !fileInput.files.length;
    confirmBtn.disabled =
      state.busy ||
      !state.preview ||
      state.preview.status !== "preview" ||
      state.preview.counts.pending === 0;
  }

  /* ---------------- upload ---------------- */

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (file) {
      dropTitle.textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KiB`;
      dropzone.classList.add("has-file");
      $("#format-error").hidden = true;
    } else {
      dropTitle.textContent = "选择或拖入 CSV 文件";
      dropzone.classList.remove("has-file");
    }
    finishUploadState();
  });

  ["dragenter", "dragover"].forEach((event) => {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((event) => {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      fileInput.dispatchEvent(new Event("change"));
    }
  });

  $("#upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || !fileInput.files.length) return;
    state.busy = true;
    finishUploadState();
    hideAlerts();
    setStatus(uploadStatus, "正在解析 CSV…", true);

    const body = new FormData();
    body.append("file", fileInput.files[0]);

    try {
      const response = await fetch("/api/previews", { method: "POST", body });
      const data = await readJson(response);

      if (response.ok) {
        state.preview = data;
        renderPreview();
        setStatus(uploadStatus, "预览已生成，请核对后确认发布。");
      } else if (data && data.error === "csv_format_error") {
        state.preview = null;
        renderFormatError(data);
        setStatus(uploadStatus, "格式校验未通过，未创建预览。");
      } else if (response.status === 409 && data && data.detail) {
        state.preview = data.detail;
        renderPreview();
        setStatus(uploadStatus, "批次存在冲突，已被整批拒绝。");
      } else {
        state.preview = null;
        renderGenericError(data);
        setStatus(uploadStatus, "上传失败，已发布数据未受影响。");
      }
    } catch (error) {
      renderGenericError({ message: `网络请求失败：${error.message}` });
      setStatus(uploadStatus, "上传失败，请检查服务状态后重试。");
    } finally {
      state.busy = false;
      finishUploadState();
    }
  });

  function hideAlerts() {
    $("#format-error").hidden = true;
    $("#publish-result").hidden = true;
    $("#stale-banner").hidden = true;
    $("#conflict-banner").hidden = true;
    $("#preview-panel").hidden = true;
  }

  function renderGenericError(data) {
    const list = $("#format-error-list");
    $("#format-error").hidden = false;
    $("#format-error-message").textContent =
      (data && data.message) || "请求未能完成。";
    list.innerHTML = "";
    const errors = data && data.detail && data.detail.errors;
    if (Array.isArray(errors)) {
      errors.forEach((item) => {
        const li = document.createElement("li");
        const row = item.row ? `第 ${item.row} 行 · ` : "";
        li.textContent = `${row}${item.message}`;
        list.appendChild(li);
      });
    }
  }

  function renderFormatError(data) {
    const detail = data.detail || {};
    const list = $("#format-error-list");
    const errors = detail.errors || [];
    $("#format-error").hidden = false;
    $("#format-error-message").textContent = data.message;
    list.innerHTML = "";
    errors.forEach((item) => {
      const li = document.createElement("li");
      const row = item.row ? `第 ${item.row} 行 · ` : "";
      li.textContent = `${row}${item.message}`;
      list.appendChild(li);
    });
    if ((detail.error_count || errors.length) > errors.length) {
      const li = document.createElement("li");
      li.textContent = `另有 ${detail.error_count - errors.length} 个错误未显示。`;
      list.appendChild(li);
    }
  }

  /* ---------------- preview rendering ---------------- */

  function renderPreview() {
    const preview = state.preview;
    if (!preview) return;
    $("#preview-panel").hidden = false;
    $("#publish-result").hidden = true;
    $("#stale-banner").hidden = preview.status !== "stale";

    const meta = $("#preview-meta");
    meta.innerHTML = "";
    const chips = [
      preview.date ? formatDate(preview.date) : "日期未知",
      preview.status === "conflict" ? "已拒绝" : `日版本 v${preview.day_version}`,
      `${preview.counts.rows} 行记录`,
    ];
    chips.forEach((text, index) => {
      const chip = document.createElement("span");
      chip.className = index === 0 ? "chip" : "chip muted";
      chip.textContent = text;
      meta.appendChild(chip);
    });

    const counts = $("#counts");
    counts.innerHTML = "";
    const countCards = [
      ["add", "新增", preview.counts.added],
      ["replace", "替换", preview.counts.replaced],
      ["ignore", "忽略", preview.counts.ignored],
      ["conflict", "冲突", preview.counts.conflicts],
      ["pending", "待发布", preview.counts.pending],
    ];
    countCards.forEach(([kind, label, value]) => {
      const card = document.createElement("div");
      card.className = `count-card ${kind}`;
      card.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
      counts.appendChild(card);
    });

    document.querySelectorAll(".tab-count").forEach((node) => {
      const key = node.dataset.count;
      node.textContent =
        key === "all" ? preview.counts.rows : preview.counts[key] ?? 0;
    });

    const conflictBanner = $("#conflict-banner");
    if (preview.status === "conflict" || preview.conflicts.length) {
      conflictBanner.hidden = false;
      renderConflictDetails(preview.conflicts);
    } else {
      conflictBanner.hidden = true;
    }

    const confirmBar = $(".confirm-bar");
    confirmBar.hidden = preview.status !== "preview";
    confirmBtn.textContent = "确认发布";
    $("#publish-result").hidden = true;
    setStatus(confirmStatus, "");

    if (preview.status === "published" && preview.result) {
      renderPublishedResult(preview.result, true);
    }

    state.activeFilter = "all";
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.action === "all";
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    renderTable();
    finishUploadState();
    $("#preview-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderConflictDetails(conflicts) {
    const holder = $("#conflict-list");
    holder.innerHTML = "";
    conflicts.forEach((conflict) => {
      const item = document.createElement("div");
      item.className = "conflict-item";
      const reasonText =
        conflict.reason === "revision_value_mismatch_with_published"
          ? `已发布修订号 r${conflict.revision} 数值为 <code>${escapeHtml(
              conflict.existing_value ?? ""
            )}</code>`
          : "本批内同一修订号对应多个数值";
      item.innerHTML = `<strong>${escapeHtml(conflict.station)} · ${escapeHtml(
        conflict.hour
      )}:00 · r${conflict.revision}</strong><br>${reasonText}；上传值：${conflict.values
        .map((value) => `<code>${escapeHtml(value)}</code>`)
        .join(" / ")}（第 ${conflict.rows.join("、")} 行）`;
      holder.appendChild(item);
    });
  }

  function renderTable() {
    const preview = state.preview;
    const tbody = $("#records-table tbody");
    tbody.innerHTML = "";
    if (!preview) return;

    const filter = state.activeFilter;
    const records =
      filter === "all"
        ? preview.records
        : preview.records.filter((record) => record.action === filter);

    $("#empty-records").hidden = records.length > 0;

    records.forEach((record) => {
      const tr = document.createElement("tr");
      const reason = REASON_LABELS[record.reason] || record.reason || "";

      let detailCell = escapeHtml(reason);
      if (record.existing) {
        detailCell += `<span class="old">原记录：r${record.existing.revision} · ${escapeHtml(
          record.existing.value
        )}</span>`;
      }
      if (record.action === "conflict") {
        detailCell = `<span class="old">${escapeHtml(reason)}</span>`;
        const matching = preview.conflicts.filter(
          (item) =>
            item.station === record.station &&
            item.hour === record.hour &&
            item.revision === record.revision
        );
        if (matching.length) {
          detailCell += `<span class="old">冲突值：${matching[0].values
            .map((value) => escapeHtml(value))
            .join(" / ")}</span>`;
        }
      }

      tr.innerHTML = `
        <td>${escapeHtml(record.station)}</td>
        <td class="mono">${escapeHtml(record.timestamp.replace("T", " "))}</td>
        <td class="num mono">r${record.revision}</td>
        <td class="num mono">${escapeHtml(record.value)}</td>
        <td><span class="badge ${record.action}">${ACTION_LABELS[record.action] || record.action}</span></td>
        <td class="reason">${detailCell}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.activeFilter = tab.dataset.action;
      document.querySelectorAll(".tab").forEach((other) => {
        const active = other === tab;
        other.classList.toggle("active", active);
        other.setAttribute("aria-selected", active ? "true" : "false");
      });
      renderTable();
    });
  });

  /* ---------------- confirmation ---------------- */

  confirmBtn.addEventListener("click", async () => {
    const preview = state.preview;
    if (!preview || !preview.preview_id || state.busy) return;
    if (
      !window.confirm(
        `确认发布 ${formatDate(preview.date)} 的 ${preview.counts.pending} 条记录？\n` +
          `新增 ${preview.counts.added} 条，替换 ${preview.counts.replaced} 条，忽略 ${preview.counts.ignored} 条。`
      )
    ) {
      return;
    }

    state.busy = true;
    confirmBtn.disabled = true;
    setStatus(confirmStatus, "正在原子发布…", true);

    try {
      const response = await fetch(
        `/api/previews/${encodeURIComponent(preview.preview_id)}/confirm`,
        { method: "POST" }
      );
      const data = await readJson(response);

      if (response.ok) {
        preview.status = "published";
        if (data.result) preview.result = data.result;
        if (data.result && typeof data.result.day_version === "number") {
          preview.day_version = data.result.day_version;
        }
        $(".confirm-bar").hidden = true;
        renderPublishedResult(data.result || data, false, data.already_published);
        setStatus(confirmStatus, "");
        await loadDays();
        if (publishedDay === preview.date) await loadDay(preview.date);
      } else if (data && data.error === "preview_stale") {
        preview.status = "stale";
        $("#stale-banner").hidden = false;
        $(".confirm-bar").hidden = true;
        setStatus(confirmStatus, "预览已失效，请重新上传。");
      } else if (data && data.error === "preview_rejected") {
        setStatus(confirmStatus, data.message || "该批次已被整批拒绝。");
      } else if (data && data.error === "preview_not_found") {
        preview.status = "expired";
        $(".confirm-bar").hidden = true;
        setStatus(confirmStatus, "预览不存在或已过期，请重新上传。");
      } else {
        setStatus(
          confirmStatus,
          (data && data.message) || "发布失败，已发布数据未受影响，请重试。"
        );
      }
    } catch (error) {
      setStatus(confirmStatus, `发布请求失败：${error.message}，请重试。`);
    } finally {
      state.busy = false;
      finishUploadState();
    }
  });

  function renderPublishedResult(result, repeated, alreadyPublished) {
    const banner = $("#publish-result");
    const title = $("#publish-result-title");
    const text = $("#publish-result-text");
    banner.hidden = false;
    banner.classList.toggle("warn", false);
    banner.classList.add("success");

    if (repeated || alreadyPublished) {
      title.textContent = "该批次已发布过，本次未重复发布";
      text.textContent = result && result.published_at
        ? `发布时间：${new Date(result.published_at).toLocaleString()}。已发布数据保持不变。`
        : "已发布数据保持不变。";
      return;
    }

    title.textContent = "发布成功";
    const published = result && result.published;
    text.textContent =
      `已写入 ${published ?? 0} 条记录：新增 ${result?.added ?? 0}，替换 ${result?.replaced ?? 0}，` +
      `忽略 ${result?.ignored ?? 0}。该日当前版本 v${result?.day_version ?? "?"}，` +
      `下游可通过 /api/days/${result?.date ?? ""} 获取 JSON。`;
  }

  /* ---------------- published days ---------------- */

  let publishedDay = null;

  async function loadDays() {
    const holder = $("#days-list");
    try {
      const response = await fetch("/api/days");
      const data = await readJson(response);
      if (!response.ok) throw new Error((data && data.message) || "无法读取日期列表");
      const days = (data && data.days) || [];
      holder.innerHTML = "";
      if (!days.length) {
        const p = document.createElement("p");
        p.className = "empty";
        p.textContent = "还没有已发布的数据。上传并确认第一批 CSV 后，这里会按日显示。";
        holder.appendChild(p);
        $("#day-viewer").hidden = true;
        return;
      }
      days.forEach((day) => {
        const row = document.createElement("div");
        row.className = "day-row";
        row.innerHTML = `
          <div>
            <div class="date">${formatDate(day.date)} <span class="mono">${day.date}</span></div>
            <div class="stats">${day.record_count} 条已发布记录 · 日版本 v${day.day_version}</div>
          </div>
          <div class="day-actions">
            <button type="button" class="ghost" data-view="${day.date}">查看 JSON</button>
            <a class="ghost" href="/api/days/${day.date}.json" download="${day.date}-pm25.json">下载</a>
          </div>
        `;
        row.querySelector("[data-view]").addEventListener("click", () => loadDay(day.date));
        holder.appendChild(row);
      });
    } catch (error) {
      holder.innerHTML = "";
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = `加载失败：${error.message}`;
      holder.appendChild(p);
    }
  }

  async function loadDay(day) {
    const viewer = $("#day-viewer");
    const pre = $("#day-json");
    const title = $("#day-title");
    const subtitle = $("#day-subtitle");
    publishedDay = day;
    try {
      const response = await fetch(`/api/days/${day}`);
      const data = await readJson(response);
      if (!response.ok) throw new Error((data && data.message) || "无法读取该日数据");
      title.textContent = `${formatDate(day)} · 已发布 JSON`;
      subtitle.textContent = `${data.count} 条记录 · 日版本 v${data.day_version}`;
      pre.textContent = JSON.stringify(data, null, 2);
      $("#download-json").href = `/api/days/${day}.json`;
      $("#download-json").download = `${day}-pm25.json`;
      $("#open-json").href = `/api/days/${day}.json`;
      viewer.hidden = false;
      viewer.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      title.textContent = `${formatDate(day)} · 加载失败`;
      subtitle.textContent = error.message;
      pre.textContent = "";
      viewer.hidden = false;
    }
  }

  $("#copy-json").addEventListener("click", async () => {
    const text = $("#day-json").textContent;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      $("#copy-json").textContent = "已复制";
      setTimeout(() => { $("#copy-json").textContent = "复制 JSON"; }, 1600);
    } catch {
      $("#copy-json").textContent = "复制失败";
    }
  });

  $("#refresh-days").addEventListener("click", loadDays);

  document.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", () => {
      document.querySelectorAll(".nav-link").forEach((node) => node.classList.remove("active"));
      link.classList.add("active");
      const target = link.getAttribute("href");
      if (target === "#published") loadDays();
      if (target === "#merge") loadStations();
    });
  });

  /* ---------------- sample ---------------- */

  $("#sample-btn").addEventListener("click", () => {
    const sample = [
      "station,timestamp,revision,value",
      "SY-001,2026-11-13T00:00:00,1,12.4",
      "SY-001,2026-11-13T01:00:00,1,10.8",
      "SY-001,2026-11-13T01:00:00,2,11.2",
      "SY-002,2026-11-13T00:00:00,3,18.0",
      "SY-002,2026-11-13T01:00:00,1,16.5",
    ].join("\r\n");
    const blob = new Blob(["﻿" + sample], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "pm25-sample-2026-11-13.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  });

  document.querySelectorAll(".alert-close").forEach((button) => {
    button.addEventListener("click", () => {
      button.closest(".alert").hidden = true;
    });
  });

  /* ---------------- station merge ---------------- */

  const SOURCE_LABELS = {
    old: "旧编号",
    new: "现用编号",
    both: "两个编号（数值一致）",
  };

  const mergeState = {
    stations: null,
    preview: null,
    busy: false,
  };

  function sourceLabel(cell) {
    const origin = cell.source_station || (cell.stations || []).join("、");
    if (cell.source === "both") return `${SOURCE_LABELS.both} · ${origin}`;
    const role = SOURCE_LABELS[cell.source];
    return role ? `${role} · ${origin}` : origin;
  }

  function mergePreviewBtnState() {
    const oldSelect = $("#merge-old");
    const newSelect = $("#merge-new");
    const chosen = oldSelect.value && newSelect.value;
    const same = oldSelect.value && oldSelect.value === newSelect.value;
    mergePreviewBtn.disabled = mergeState.busy || !chosen || same;
    return { chosen, same };
  }

  const mergePreviewBtn = $("#merge-preview-btn");

  function fillStationSelect(select, stations, placeholder) {
    const previous = select.value;
    select.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = placeholder;
    select.appendChild(blank);
    stations.forEach((station) => {
      const option = document.createElement("option");
      option.value = station;
      option.textContent = station;
      select.appendChild(option);
    });
    if (stations.includes(previous)) select.value = previous;
  }

  async function loadStations() {
    const status = $("#merge-status");
    try {
      const response = await fetch("/api/stations");
      const data = await readJson(response);
      if (!response.ok) throw new Error((data && data.message) || "无法读取站点列表");
      mergeState.stations = data;
      const names = (data.available || []).map((item) => item.station);
      fillStationSelect($("#merge-old"), names, "选择旧编号");
      fillStationSelect($("#merge-new"), names, "选择现用编号");
      renderMergeGroups(data.merges || []);
      fillMergedSelects(data.merges || []);
      if (!names.length) {
        setStatus(status, "暂时没有可归并的站点，请先发布数据。");
      } else {
        setStatus(status, "");
      }
    } catch (error) {
      setStatus(status, `站点列表加载失败：${error.message}`);
      renderMergeGroups([]);
    } finally {
      mergePreviewBtnState();
    }
  }

  function renderMergeGroups(merges) {
    const holder = $("#merge-groups-list");
    holder.innerHTML = "";
    if (!merges.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "还没有已归并的站点。";
      holder.appendChild(p);
      return;
    }
    merges.forEach((group) => {
      const row = document.createElement("div");
      row.className = "day-row";
      const former = group.former_stations.join("、") || "—";
      row.innerHTML = `
        <div>
          <div class="date">${escapeHtml(group.current_station)} <span class="mono">现用编号</span></div>
          <div class="stats">原编号：${escapeHtml(former)} · 归并时间 ${escapeHtml(
            new Date(group.merged_at).toLocaleString()
          )}</div>
        </div>
      `;
      holder.appendChild(row);
    });
  }

  function fillMergedSelects(merges) {
    const select = $("#merged-station");
    const previous = select.value;
    select.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = merges.length ? "选择站点" : "暂无已归并站点";
    select.appendChild(blank);
    merges.forEach((group) => {
      ["current", ...group.former_stations].forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent =
          name === group.current_station ? `${name}（现用）` : `${name}（原编号）`;
        select.appendChild(option);
      });
    });
    if ([...select.options].some((option) => option.value === previous)) {
      select.value = previous;
    }
    fillMergedDates();
  }

  function fillMergedDates() {
    const stationSelect = $("#merged-station");
    const dateSelect = $("#merged-date");
    const merges = (mergeState.stations && mergeState.stations.merges) || [];
    const group = merges.find(
      (item) =>
        item.current_station === stationSelect.value ||
        item.former_stations.includes(stationSelect.value)
    );
    const previous = dateSelect.value;
    dateSelect.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "选择日期";
    dateSelect.appendChild(blank);

    if (!group) {
      $("#merged-load-btn").disabled = true;
      return Promise.resolve([]);
    }

    return fetch(`/api/stations/${encodeURIComponent(group.current_station)}`)
      .then((response) => readJson(response))
      .then((data) => (data && data.dates) || [])
      .catch(() => [])
      .then((dates) => {
        dates.forEach((day) => {
          const option = document.createElement("option");
          option.value = day;
          option.textContent = day;
          dateSelect.appendChild(option);
        });
        if (dates.includes(previous)) dateSelect.value = previous;
        $("#merged-load-btn").disabled = !(stationSelect.value && dateSelect.value);
      });
  }

  $("#refresh-stations").addEventListener("click", loadStations);
  $("#merge-old").addEventListener("change", mergePreviewBtnState);
  $("#merge-new").addEventListener("change", mergePreviewBtnState);

  $("#merge-preview-btn").addEventListener("click", async () => {
    const { chosen, same } = mergePreviewBtnState();
    if (!chosen || same || mergeState.busy) return;
    mergeState.busy = true;
    mergePreviewBtnState();
    $("#merge-conflict").hidden = true;
    $("#merge-stale").hidden = true;
    $("#merge-result").hidden = true;
    $("#merge-preview").hidden = true;
    const status = $("#merge-status");
    setStatus(status, "正在归并预览…", true);

    try {
      const response = await fetch("/api/merges", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          old_station: $("#merge-old").value,
          new_station: $("#merge-new").value,
        }),
      });
      const data = await readJson(response);
      if (response.ok) {
        mergeState.preview = data;
        renderMergePreview(data);
        setStatus(status, "归并预览已生成，请核对后确认。");
      } else if (response.status === 409 && data && data.error === "merge_conflict") {
        mergeState.preview = data.detail;
        renderMergeConflict(data.detail);
        setStatus(status, "存在冲突，归并已拒绝。");
      } else if (response.status === 409) {
        mergeState.preview = null;
        renderMergeConflict(null);
        setStatus(status, (data && data.message) || "归并请求被拒绝。");
        $("#merge-conflict-list").innerHTML = "";
      } else {
        mergeState.preview = null;
        setStatus(status, (data && data.message) || "生成归并预览失败。");
      }
    } catch (error) {
      setStatus(status, `生成归并预览失败：${error.message}`);
    } finally {
      mergeState.busy = false;
      mergePreviewBtnState();
    }
  });

  function renderMergeConflict(detail) {
    const banner = $("#merge-conflict");
    const list = $("#merge-conflict-list");
    list.innerHTML = "";
    banner.hidden = false;
    if (!detail || !(detail.conflicts || []).length) {
      if (detail !== null) {
        const item = document.createElement("div");
        item.className = "conflict-item";
        item.textContent = "无法生成归并预览。";
        list.appendChild(item);
      }
      return;
    }
    detail.conflicts.forEach((conflict) => {
      const item = document.createElement("div");
      item.className = "conflict-item";
      const detailText = Object.entries(conflict.detail || {})
        .map(([station, value]) => `${escapeHtml(station)}：<code>${escapeHtml(value)}</code>`)
        .join("；");
      item.innerHTML =
        `<strong>${escapeHtml(conflict.date)} ${escapeHtml(conflict.hour)}:00 · r${conflict.revision}</strong><br>` +
        `同一小时的最高修订号数值不一致。${detailText}`;
      list.appendChild(item);
    });
  }

  function renderMergePreview(preview) {
    mergeState.preview = preview;
    const panel = $("#merge-preview");
    panel.hidden = false;
    $("#merge-conflict").hidden = true;
    $("#merge-stale").hidden = preview.status !== "stale";
    $("#merge-result").hidden = true;
    $("#merge-title").textContent = `${preview.old_station} → ${preview.new_station}`;
    $("#merge-subtitle").textContent =
      "合并后按现用编号读取；下方“来源编号”标出每条读数原本属于哪个编号。";

    const meta = $("#merge-meta");
    meta.innerHTML = "";
    [
      `现用编号 ${preview.current_station}`,
      `${preview.counts.cells} 个小时`,
      `${preview.dates.length} 个日期`,
    ].forEach((text, index) => {
      const chip = document.createElement("span");
      chip.className = index === 0 ? "chip" : "chip muted";
      chip.textContent = text;
      meta.appendChild(chip);
    });

    const counts = $("#merge-counts");
    counts.innerHTML = "";
    [
      ["old", "来自旧编号", preview.counts.from_old],
      ["new", "来自现用编号", preview.counts.from_new],
      ["both", "数值一致", preview.counts.from_both],
      ["conflict", "冲突", preview.counts.conflicts],
    ].forEach(([kind, label, value]) => {
      const card = document.createElement("div");
      card.className = `count-card ${kind}`;
      card.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
      counts.appendChild(card);
    });

    const tbody = $("#merge-table tbody");
    tbody.innerHTML = "";
    const cells = preview.cells || [];
    $("#merge-empty").hidden = cells.length > 0;
    cells.forEach((cell) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${escapeHtml(cell.date)}</td>
        <td class="mono">${escapeHtml(cell.timestamp.replace("T", " "))}</td>
        <td class="num mono">r${cell.revision}</td>
        <td class="num mono">${cell.value === null ? "—" : escapeHtml(cell.value)}</td>
        <td class="reason">${escapeHtml(sourceLabel(cell))}</td>
      `;
      tbody.appendChild(tr);
    });

    const canConfirm =
      preview.status === "preview" && preview.counts.conflicts === 0;
    $("#merge-confirm-btn").disabled = mergeState.busy || !canConfirm;
    $("#merge-confirm-btn").textContent = "确认归并";
    setStatus($("#merge-confirm-status"), "");
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("#merge-confirm-btn").addEventListener("click", async () => {
    const preview = mergeState.preview;
    if (!preview || !preview.merge_id || mergeState.busy) return;
    if (
      !window.confirm(
        `确认把 ${preview.old_station} 归并到现用编号 ${preview.new_station}？\n` +
          `合并 ${preview.counts.cells} 个小时读数，原始已发布记录不会改动。`
      )
    ) {
      return;
    }
    mergeState.busy = true;
    $("#merge-confirm-btn").disabled = true;
    const status = $("#merge-confirm-status");
    setStatus(status, "正在原子归并…", true);

    try {
      const response = await fetch(
        `/api/merges/${encodeURIComponent(preview.merge_id)}/confirm`,
        { method: "POST" }
      );
      const data = await readJson(response);
      if (response.ok) {
        preview.status = "merged";
        $("#merge-confirm-btn").disabled = true;
        const result = (data && data.result) || {};
        $("#merge-result").hidden = false;
        $("#merge-result-title").textContent = data.already_merged
          ? "该归并已确认过，本次未重复建立"
          : "归并成功";
        $("#merge-result-text").textContent =
          `${result.old_station || preview.old_station} 已并入 ${result.new_station || preview.new_station}。` +
          "原始已发布记录未改动，此后任一编号的补报都会按合并身份预览与发布。";
        setStatus(status, "");
        await loadStations();
        await loadDays();
      } else if (data && data.error === "merge_stale") {
        preview.status = "stale";
        $("#merge-stale").hidden = false;
        $("#merge-confirm-btn").disabled = true;
        setStatus(status, "归并预览已失效，请重新生成。");
      } else if (data && data.error === "station_already_merged") {
        $("#merge-confirm-btn").disabled = true;
        setStatus(status, data.message || "站点编号已参与归并。");
        await loadStations();
      } else {
        setStatus(status, (data && data.message) || "归并失败，已发布数据未受影响。");
      }
    } catch (error) {
      setStatus(status, `归并请求失败：${error.message}，请重试。`);
    } finally {
      mergeState.busy = false;
      mergePreviewBtnState();
    }
  });

  /* ---------------- merged readings ---------------- */

  $("#merged-station").addEventListener("change", fillMergedDates);
  $("#merged-date").addEventListener("change", () => {
    $("#merged-load-btn").disabled = !($("#merged-station").value && $("#merged-date").value);
  });

  $("#merged-load-btn").addEventListener("click", async () => {
    const station = $("#merged-station").value;
    const day = $("#merged-date").value;
    if (!station || !day) return;
    const status = $("#merged-status");
    setStatus(status, "正在读取合并读数…", true);
    try {
      const response = await fetch(
        `/api/merged/${encodeURIComponent(station)}/days/${encodeURIComponent(day)}`
      );
      const data = await readJson(response);
      if (!response.ok) throw new Error((data && data.message) || "无法读取合并读数");
      renderMergedDay(station, day, data);
      setStatus(status, "");
    } catch (error) {
      setStatus(status, `读取失败：${error.message}`);
    }
  });

  function renderMergedDay(station, day, data) {
    const viewer = $("#merged-viewer");
    viewer.hidden = false;
    const canonical = data.current_station || station;
    const tbody = $("#merged-table tbody");
    tbody.innerHTML = "";
    const records = data.records || [];
    $("#merged-empty").hidden = records.length > 0;
    records.forEach((record) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${escapeHtml(record.timestamp.replace("T", " "))}</td>
        <td class="num mono">r${record.revision}</td>
        <td class="num mono">${escapeHtml(record.value)}</td>
        <td class="reason">${escapeHtml(
          record.source_station || (record.sources || []).join("、")
        )}</td>
      `;
      tbody.appendChild(tr);
    });
    const url = `/api/merged/${encodeURIComponent(canonical)}/days/${encodeURIComponent(day)}`;
    $("#merged-download").hidden = false;
    $("#merged-download").href = `${url}.json`;
    $("#merged-download").download = `${canonical}-${day}-pm25.json`;
    $("#merged-open").hidden = false;
    $("#merged-open").href = `${url}.json`;
    $("#merged-json").textContent = JSON.stringify(data, null, 2);
    viewer.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  finishUploadState();
  loadDays();
  loadStations();
})();
