const escaped = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

let api = null;
let catalog = {};
let canvas = { min_width: 480, max_width: 1600, min_height: 0, max_height: 4000, default_width: 640, max_customs: 8 };
let layout = {};
let currentType = "daily";
let selected = "";
let previewTimer = 0;
let lastBoxes = [];

function $(selector, root = document) {
  return root.querySelector(selector);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function parseCoord(value) {
  if (value === "" || value == null) return null;
  const number = Number.parseInt(value, 10);
  return Number.isFinite(number) ? number : null;
}

function currentCard() {
  layout[currentType] ||= { note: "", badge: "", width: 640, height: 0, elements: {}, customs: [] };
  const card = layout[currentType];
  card.elements ||= {};
  card.customs ||= [];
  return card;
}

function renderTypeSelect() {
  const select = $("#layout-type");
  if (!select) return;
  select.innerHTML = Object.entries(catalog)
    .map(
      ([id, spec]) =>
        `<option value="${escaped(id)}"${id === currentType ? " selected" : ""}>${escaped(spec.label)}</option>`,
    )
    .join("");
}

function renderCanvas() {
  const card = currentCard();
  const width = $("#layout-width");
  const height = $("#layout-height");
  if (width) {
    width.min = canvas.min_width;
    width.max = canvas.max_width;
    width.value = card.width || canvas.default_width || 640;
  }
  if (height) {
    height.min = canvas.min_height;
    height.max = canvas.max_height;
    height.value = card.height || 0;
  }
}

function renderElements() {
  const host = $("#layout-elements");
  const spec = catalog[currentType];
  const card = currentCard();
  if (!host || !spec) return;
  const groups = new Map();
  for (const element of spec.elements) {
    if (!groups.has(element.group)) groups.set(element.group, []);
    groups.get(element.group).push(element);
    card.elements[element.id] ||= { visible: true, x: null, y: null };
  }
  host.innerHTML = [...groups.entries()]
    .map(([group, items]) => {
      const rows = items
        .map((item) => {
          const state = card.elements[item.id];
          const active = selected === `el:${item.id}` ? " is-selected" : "";
          return `<label class="layout-row${active}" data-layout-select="el:${escaped(item.id)}">
            <input type="checkbox" data-layout-el="${escaped(item.id)}" data-layout-field="visible"${state.visible === false ? "" : " checked"}>
            <span>${escaped(item.label)}</span>
            <input type="number" data-layout-el="${escaped(item.id)}" data-layout-field="x" placeholder="X 自动" value="${escaped(state.x ?? "")}">
            <input type="number" data-layout-el="${escaped(item.id)}" data-layout-field="y" placeholder="Y 自动" value="${escaped(state.y ?? "")}">
          </label>`;
        })
        .join("");
      return `<section class="layout-group"><h4>${escaped(group)}</h4>${rows}</section>`;
    })
    .join("");
  const note = $("#layout-note");
  const badge = $("#layout-badge");
  if (note) note.value = card.note || "";
  if (badge) badge.value = card.badge || "";
  renderCustoms();
}

function renderCustoms() {
  const host = $("#layout-custom-list");
  const card = currentCard();
  if (!host) return;
  host.innerHTML = (card.customs || [])
    .map((item, index) => {
      const active = selected === `custom:${item.id}` ? " is-selected" : "";
      return `<div class="layout-custom${active}" data-layout-select="custom:${escaped(item.id)}">
        <input type="checkbox" data-custom-id="${escaped(item.id)}" data-custom-field="visible"${item.visible === false ? "" : " checked"}>
        <input type="text" maxlength="40" data-custom-id="${escaped(item.id)}" data-custom-field="text" placeholder="第 ${index + 1} 条文字" value="${escaped(item.text || "")}">
        <input type="number" data-custom-id="${escaped(item.id)}" data-custom-field="x" placeholder="X" value="${escaped(item.x ?? "")}">
        <input type="number" data-custom-id="${escaped(item.id)}" data-custom-field="y" placeholder="Y" value="${escaped(item.y ?? "")}">
        <button type="button" class="button ghost" data-custom-remove="${escaped(item.id)}">删除</button>
      </div>`;
    })
    .join("") || '<p class="muted">还没有额外文字。添加后可点预览图放置。</p>';
}

function readForm() {
  const spec = catalog[currentType];
  const card = {
    note: ($("#layout-note")?.value || "").trim(),
    badge: ($("#layout-badge")?.value || "").trim(),
    width: parseCoord($("#layout-width")?.value) || canvas.default_width || 640,
    height: parseCoord($("#layout-height")?.value) || 0,
    elements: {},
    customs: [],
  };
  if (spec) {
    for (const item of spec.elements) {
      const visible = $(`[data-layout-el="${item.id}"][data-layout-field="visible"]`);
      const x = $(`[data-layout-el="${item.id}"][data-layout-field="x"]`);
      const y = $(`[data-layout-el="${item.id}"][data-layout-field="y"]`);
      card.elements[item.id] = {
        visible: Boolean(visible?.checked),
        x: parseCoord(x?.value),
        y: parseCoord(y?.value),
      };
    }
  }
  for (const node of document.querySelectorAll("[data-custom-id][data-custom-field='text']")) {
    const id = node.dataset.customId;
    card.customs.push({
      id,
      text: node.value.trim(),
      visible: Boolean($(`[data-custom-id="${id}"][data-custom-field="visible"]`)?.checked),
      x: parseCoord($(`[data-custom-id="${id}"][data-custom-field="x"]`)?.value),
      y: parseCoord($(`[data-custom-id="${id}"][data-custom-field="y"]`)?.value),
    });
  }
  layout[currentType] = card;
  return card;
}

function setStatus(text) {
  const node = $("#layout-status");
  if (node) node.textContent = text;
}

async function refreshPreview() {
  if (!api) return;
  readForm();
  setStatus("正在生成预览…");
  const result = await api("/api/card-layout/preview", "POST", {
    type: currentType,
    card: currentCard(),
  });
  const image = $("#layout-preview-image");
  if (image && result.image) {
    image.src = result.image;
    image.hidden = false;
    lastBoxes = result.boxes || [];
    if (image.complete) renderHandles();
    else image.onload = () => renderHandles();
  }
  setStatus(selected ? "可拖动预览图上的色块，松手后刷新。" : "拖动色块，或先点左侧元素再点图放置。");
}

function boxLabel(id) {
  const spec = catalog[currentType];
  const found = spec?.elements?.find((item) => item.id === id);
  if (found) return found.label;
  const custom = (currentCard().customs || []).find((item) => item.id === id);
  return custom?.text || id;
}

function renderHandles() {
  const layer = $("#layout-handles");
  const image = $("#layout-preview-image");
  if (!layer || !image?.naturalWidth) return;
  const scale = image.clientWidth / image.naturalWidth;
  layer.innerHTML = lastBoxes
    .map((box) => {
      const active = selected === `el:${box.id}` || selected === `custom:${box.id}` ? " is-selected" : "";
      return `<button type="button" class="layout-handle${active}" data-handle-id="${escaped(box.id)}"
        style="left:${box.x * scale}px;top:${box.y * scale}px;width:${Math.max(28, box.w * scale)}px;height:${Math.max(18, box.h * scale)}px">${escaped(boxLabel(box.id))}</button>`;
    })
    .join("");
}

function pointerToCanvas(event, image) {
  const rect = image.getBoundingClientRect();
  const x = Math.round(((event.clientX - rect.left) * image.naturalWidth) / rect.width);
  const y = Math.round(((event.clientY - rect.top) * image.naturalHeight) / rect.height);
  return { x, y };
}

function startHandleDrag(event) {
  const handle = event.target.closest("[data-handle-id]");
  const image = $("#layout-preview-image");
  if (!handle || !image?.naturalWidth) return;
  event.preventDefault();
  const id = handle.dataset.handleId;
  selected = id.startsWith("custom_") ? `custom:${id}` : `el:${id}`;
  renderElements();
  const scale = image.clientWidth / image.naturalWidth;
  const start = pointerToCanvas(event, image);
  const origLeft = parseFloat(handle.style.left) || 0;
  const origTop = parseFloat(handle.style.top) || 0;
  let moved = false;
  handle.classList.add("is-dragging");
  const move = (next) => {
    const point = pointerToCanvas(next, image);
    const dx = (point.x - start.x) * scale;
    const dy = (point.y - start.y) * scale;
    if (Math.abs(point.x - start.x) + Math.abs(point.y - start.y) > 2) moved = true;
    handle.style.left = `${origLeft + dx}px`;
    handle.style.top = `${origTop + dy}px`;
  };
  const up = (next) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    handle.classList.remove("is-dragging");
    const point = pointerToCanvas(next, image);
    if (moved) placeAt(Math.max(0, point.x), Math.max(0, point.y));
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function schedulePreview() {
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(() => {
    refreshPreview().catch((error) => setStatus(error.message || "预览失败"));
  }, 280);
}

function selectTarget(target) {
  selected = target;
  renderElements();
  setStatus(target ? "已选中元素，点击预览图放置。" : "先点左侧一个元素，再点预览图放置。");
}

function placeAt(x, y) {
  readForm();
  const card = currentCard();
  if (selected.startsWith("el:")) {
    const id = selected.slice(3);
    card.elements[id] ||= { visible: true, x: null, y: null };
    card.elements[id].x = x;
    card.elements[id].y = y;
    card.elements[id].visible = true;
  } else if (selected.startsWith("custom:")) {
    const id = selected.slice(7);
    const item = card.customs.find((entry) => entry.id === id);
    if (!item) return;
    item.x = x;
    item.y = y;
    item.visible = true;
  } else {
    setStatus("先点左侧一个元素，再点预览图放置。");
    return;
  }
  renderElements();
  schedulePreview();
}

export async function loadCardLayout(client) {
  api = client;
  const result = await api("/api/card-layout");
  catalog = result.catalog || {};
  canvas = { ...canvas, ...(result.canvas || {}) };
  layout = clone(result.layout || {});
  if (!catalog[currentType]) currentType = Object.keys(catalog)[0] || "daily";
  renderTypeSelect();
  renderCanvas();
  renderElements();
}

export function installCardLayout(client) {
  api = client;
  const type = $("#layout-type");
  type?.addEventListener("change", () => {
    readForm();
    currentType = type.value;
    selected = "";
    renderCanvas();
    renderElements();
    schedulePreview();
  });
  $("#layout-elements")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-layout-select]");
    if (row) selectTarget(row.dataset.layoutSelect);
  });
  $("#layout-elements")?.addEventListener("input", schedulePreview);
  $("#layout-elements")?.addEventListener("change", schedulePreview);
  $("#layout-note")?.addEventListener("input", schedulePreview);
  $("#layout-badge")?.addEventListener("input", schedulePreview);
  $("#layout-width")?.addEventListener("input", schedulePreview);
  $("#layout-height")?.addEventListener("input", schedulePreview);
  $("#layout-custom-list")?.addEventListener("input", schedulePreview);
  $("#layout-custom-list")?.addEventListener("change", schedulePreview);
  $("#layout-custom-list")?.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-custom-remove]");
    if (remove) {
      readForm();
      const card = currentCard();
      card.customs = card.customs.filter((item) => item.id !== remove.dataset.customRemove);
      if (selected === `custom:${remove.dataset.customRemove}`) selected = "";
      renderElements();
      schedulePreview();
      return;
    }
    const row = event.target.closest("[data-layout-select]");
    if (row) selectTarget(row.dataset.layoutSelect);
  });
  $("#layout-add-custom")?.addEventListener("click", () => {
    readForm();
    const card = currentCard();
    if (card.customs.length >= (canvas.max_customs || 8)) {
      setStatus(`最多添加 ${canvas.max_customs || 8} 条自定义文字。`);
      return;
    }
    const used = new Set(card.customs.map((item) => item.id));
    let index = card.customs.length + 1;
    while (used.has(`custom_${index}`)) index += 1;
    const id = `custom_${index}`;
    card.customs.push({ id, text: "新文字", visible: true, x: 40, y: 80 + card.customs.length * 28 });
    selected = `custom:${id}`;
    renderElements();
    schedulePreview();
  });
  $("#layout-handles")?.addEventListener("pointerdown", startHandleDrag);
  $("#layout-preview-image")?.addEventListener("click", (event) => {
    const image = event.currentTarget;
    if (!image.naturalWidth || !image.clientWidth) return;
    const x = Math.round((event.offsetX * image.naturalWidth) / image.clientWidth);
    const y = Math.round((event.offsetY * image.naturalHeight) / image.clientHeight);
    placeAt(x, y);
  });
  window.addEventListener("resize", () => renderHandles());
  $("#layout-preview")?.addEventListener("click", () => {
    refreshPreview().catch((error) => setStatus(error.message || "预览失败"));
  });
  $("#layout-reset")?.addEventListener("click", async () => {
    const result = await api("/api/card-layout");
    layout[currentType] = clone(result.defaults[currentType]);
    selected = "";
    renderCanvas();
    renderElements();
    await refreshPreview();
    setStatus("已恢复此卡片的默认排版，尚未保存。");
  });
  $("#layout-save")?.addEventListener("click", async () => {
    readForm();
    const result = await api("/api/card-layout", "POST", { layout });
    layout = clone(result.layout || layout);
    setStatus("排版已保存，之后的推送图会按此生成。");
  });
}

export function maybePreviewCardLayout(tab) {
  if (tab === "layout") {
    refreshPreview().catch((error) => setStatus(error.message || "预览失败"));
  }
}
