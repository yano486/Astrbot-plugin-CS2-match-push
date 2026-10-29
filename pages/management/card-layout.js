const escaped = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

let api = null;
let catalog = {};
let layout = {};
let currentType = "daily";
let previewTimer = 0;

function $(selector, root = document) {
  return root.querySelector(selector);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function currentCard() {
  layout[currentType] ||= { note: "", elements: {} };
  layout[currentType].elements ||= {};
  return layout[currentType];
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
          const x = state.x ?? "";
          const y = state.y ?? "";
          return `<label class="layout-row">
            <input type="checkbox" data-layout-el="${escaped(item.id)}" data-layout-field="visible"${state.visible === false ? "" : " checked"}>
            <span>${escaped(item.label)}</span>
            <input type="number" data-layout-el="${escaped(item.id)}" data-layout-field="x" placeholder="X 自动" value="${escaped(x)}">
            <input type="number" data-layout-el="${escaped(item.id)}" data-layout-field="y" placeholder="Y 自动" value="${escaped(y)}">
          </label>`;
        })
        .join("");
      return `<section class="layout-group"><h4>${escaped(group)}</h4>${rows}</section>`;
    })
    .join("");
  const note = $("#layout-note");
  if (note) note.value = card.note || "";
}

function readForm() {
  const card = { note: ($("#layout-note")?.value || "").trim(), elements: {} };
  const spec = catalog[currentType];
  if (!spec) return card;
  for (const item of spec.elements) {
    const visible = $(`[data-layout-el="${item.id}"][data-layout-field="visible"]`);
    const x = $(`[data-layout-el="${item.id}"][data-layout-field="x"]`);
    const y = $(`[data-layout-el="${item.id}"][data-layout-field="y"]`);
    card.elements[item.id] = {
      visible: Boolean(visible?.checked),
      x: x?.value === "" ? null : Number(x.value),
      y: y?.value === "" ? null : Number(y.value),
    };
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
  }
  setStatus("预览仅用于排版，不会发送到 QQ。");
}

function schedulePreview() {
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(() => {
    refreshPreview().catch((error) => setStatus(error.message || "预览失败"));
  }, 280);
}

export async function loadCardLayout(client) {
  api = client;
  const result = await api("/api/card-layout");
  catalog = result.catalog || {};
  layout = clone(result.layout || {});
  if (!catalog[currentType]) currentType = Object.keys(catalog)[0] || "daily";
  renderTypeSelect();
  renderElements();
}

export function installCardLayout(client) {
  api = client;
  const type = $("#layout-type");
  type?.addEventListener("change", () => {
    readForm();
    currentType = type.value;
    renderElements();
    schedulePreview();
  });
  $("#layout-elements")?.addEventListener("input", schedulePreview);
  $("#layout-elements")?.addEventListener("change", schedulePreview);
  $("#layout-note")?.addEventListener("input", schedulePreview);
  $("#layout-preview")?.addEventListener("click", () => {
    refreshPreview().catch((error) => setStatus(error.message || "预览失败"));
  });
  $("#layout-reset")?.addEventListener("click", async () => {
    const result = await api("/api/card-layout");
    layout[currentType] = clone(result.defaults[currentType]);
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
