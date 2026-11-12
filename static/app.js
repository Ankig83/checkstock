const state = { stock: null, operations: [] };
let lastRequest = null;
let transferDefaulted = false;

const notice = document.querySelector("#notice");

function esc(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function noticeHtml(message, details, withReplay) {
  let html = `<p>${esc(message)}</p>`;
  const elsewhere = details && details.available_elsewhere;
  if (elsewhere && elsewhere.length) {
    html += `<ul>${elsewhere.map((item) => `<li>${esc(item.name)}: доступно ${item.available} шт.</li>`).join("")}</ul>`;
  }
  if (details && details.available_on_archived_warehouses) {
    html += `<p>На архивных складах ещё ${details.available_on_archived_warehouses} шт. Этот запрос их не списывает.</p>`;
  }
  if (withReplay) {
    html += `<button type="button" data-action="replay">Повторить подтверждение с тем же ключом</button>`;
  }
  return html;
}

function showNotice(kind, html) {
  notice.hidden = false;
  notice.className = `notice ${kind}`;
  notice.innerHTML = html;
}

function parseError(data) {
  if (!data) return { message: "Нет связи с сервером", details: {} };
  if (data.message) return { message: data.message, details: data.details || {} };
  if (typeof data.detail === "string") return { message: data.detail, details: {} };
  if (Array.isArray(data.detail)) {
    return { message: data.detail.map((item) => item.msg).join("; ") || "Проверьте поля формы", details: {} };
  }
  return { message: "Не удалось выполнить операцию", details: {} };
}

async function api(method, url, body, key) {
  const headers = {};
  const options = { method, headers };
  if (key) headers["Idempotency-Key"] = key;
  if (body !== undefined && body !== null) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error("request failed");
    error.data = data;
    throw error;
  }
  return data;
}

function showSuccess(data) {
  const replay = Boolean(data.idempotent_replay);
  const message = replay
    ? `Повторное подтверждение. Новая проводка не создана, остатки не изменились. ${data.message}`
    : data.message;
  showNotice(replay ? "replay" : "ok", noticeHtml(message, null, true));
}

function showFailure(error) {
  const parsed = parseError(error && error.data);
  showNotice("bad", noticeHtml(parsed.message, parsed.details, false));
}

async function submit(method, url, body) {
  if (document.body.dataset.busy === "1") return;
  document.body.dataset.busy = "1";
  const key = crypto.randomUUID();
  try {
    const data = await api(method, url, body ?? null, key);
    lastRequest = { method, url, body: body ?? null, key };
    showSuccess(data);
    await refresh();
  } catch (error) {
    showFailure(error);
  } finally {
    document.body.dataset.busy = "";
  }
}

async function replayLast() {
  if (!lastRequest || document.body.dataset.busy === "1") return;
  document.body.dataset.busy = "1";
  try {
    const data = await api(lastRequest.method, lastRequest.url, lastRequest.body, lastRequest.key);
    showSuccess(data);
    await refresh();
  } catch (error) {
    showFailure(error);
  } finally {
    document.body.dataset.busy = "";
  }
}

function integer(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function productById(stock, id) {
  return stock.products.find((product) => product.id === id);
}

function warehouseById(stock, id) {
  return stock.warehouses.find((warehouse) => warehouse.id === id);
}

function warehouseLabel(warehouse) {
  if (!warehouse) return "—";
  return warehouse.status === "archived" ? `${warehouse.name} (архив)` : warehouse.name;
}

function balanceMap(stock) {
  const map = new Map();
  for (const row of stock.balances) map.set(`${row.warehouse_id}:${row.product_id}`, row);
  return map;
}

function readBalance(map, warehouseId, productId) {
  return map.get(`${warehouseId}:${productId}`) || { on_hand: 0, reserved_fbs: 0, available: 0 };
}

function fillSelect(select, items) {
  const previous = select.value;
  select.replaceChildren();
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = item.label;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

function fillForms(stock) {
  const active = stock.warehouses.filter((warehouse) => warehouse.status === "active");
  const warehouseOptions = active.map((warehouse) => ({ id: warehouse.id, label: warehouse.name }));
  const productOptions = stock.products.map((product) => ({ id: product.id, label: `${product.sku} ${product.name}` }));
  for (const select of document.querySelectorAll("select[name='warehouse_id'], select[name='from_warehouse_id']")) {
    fillSelect(select, warehouseOptions);
  }
  fillSelect(document.querySelector("#form-transfer select[name='to_warehouse_id']"), warehouseOptions);
  for (const select of document.querySelectorAll("select[name='product_id']")) fillSelect(select, productOptions);
  const destination = document.querySelector("#form-transfer select[name='to_warehouse_id']");
  if (!transferDefaulted && destination.options.length > 1) {
    destination.selectedIndex = 1;
    transferDefaulted = true;
  }
}

function renderRecon(stock) {
  const box = document.querySelector("#recon");
  const parts = stock.reconciliation.products.map((row) => (
    `${row.sku}: приход ${row.received} = склады ${row.on_hand} + в пути ${row.in_transit} + маркетплейс ${row.marketplace}`
  ));
  box.className = stock.reconciliation.ok ? "recon ok" : "recon bad";
  box.textContent = `${stock.reconciliation.ok ? "Остатки сходятся" : "Остатки не сходятся"}. ${parts.join(" · ")}`;
}

function renderStock(stock) {
  const map = balanceMap(stock);
  const wrap = document.querySelector("#stock-table");
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const title of ["Артикул", "Товар"]) {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.append(cell);
  }
  for (const warehouse of stock.warehouses) {
    const cell = document.createElement("th");
    cell.textContent = warehouseLabel(warehouse);
    if (warehouse.status === "archived") cell.className = "archived";
    head.append(cell);
  }
  for (const title of ["Между ФФ", "FBO в пути", "К клиенту", "Маркетплейс"]) {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.append(cell);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  const body = document.createElement("tbody");

  function qtyCell(value) {
    const cell = document.createElement("td");
    cell.textContent = String(value);
    return cell;
  }

  for (const product of stock.products) {
    const row = document.createElement("tr");
    const sku = document.createElement("td");
    sku.textContent = product.sku;
    const name = document.createElement("td");
    name.textContent = product.name;
    row.append(sku, name);
    for (const warehouse of stock.warehouses) {
      const numbers = readBalance(map, warehouse.id, product.id);
      const cell = document.createElement("td");
      if (warehouse.status === "archived") cell.className = "archived";
      const stack = document.createElement("div");
      stack.className = "stack";
      const lines = [
        ["физ.", numbers.on_hand, ""],
        ["дост.", numbers.available, ""],
        ["рез.", numbers.reserved_fbs, numbers.reserved_fbs ? "rs" : ""],
      ];
      for (const [label, value, className] of lines) {
        const span = document.createElement("span");
        if (className) span.className = className;
        const caption = document.createElement("em");
        caption.textContent = label;
        span.append(caption, document.createTextNode(String(value)));
        stack.append(span);
      }
      cell.append(stack);
      row.append(cell);
    }
    const between = stock.transfers.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
    const fbo = stock.fbo_shipments.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
    const fbs = stock.fbs_shipments.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
    const market = stock.marketplace.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
    row.append(qtyCell(between), qtyCell(fbo), qtyCell(fbs), qtyCell(market));
    body.append(row);
  }
  table.append(thead, body);
  wrap.replaceChildren(table);
}

function whereText(stock, product) {
  const map = balanceMap(stock);
  const parts = [];
  for (const warehouse of stock.warehouses) {
    const row = readBalance(map, warehouse.id, product.id);
    if (!row.on_hand && !row.reserved_fbs) continue;
    const reserve = row.reserved_fbs ? `, резерв ${row.reserved_fbs}` : "";
    parts.push(`${warehouseLabel(warehouse)}: ${row.on_hand} шт.${reserve}`);
  }
  for (const shipment of stock.transfers) {
    if (shipment.product_id !== product.id) continue;
    parts.push(`в пути ${warehouseLabel(warehouseById(stock, shipment.from_warehouse_id))} → ${warehouseLabel(warehouseById(stock, shipment.to_warehouse_id))}: ${shipment.qty} шт.`);
  }
  const fbo = stock.fbo_shipments.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
  const fbs = stock.fbs_shipments.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
  const market = stock.marketplace.filter((item) => item.product_id === product.id).reduce((sum, item) => sum + item.qty, 0);
  if (fbo) parts.push(`в пути на маркетплейс: ${fbo} шт.`);
  if (fbs) parts.push(`к клиенту: ${fbs} шт.`);
  if (market) parts.push(`на маркетплейсе: ${market} шт.`);
  return parts.join(" · ") || "не числится";
}

function renderWhere(stock) {
  for (const id of ["where-stock", "where-moves"]) {
    const root = document.getElementById(id);
    root.replaceChildren();
    for (const product of stock.products) {
      const line = document.createElement("p");
      const title = document.createElement("strong");
      title.textContent = `${product.sku} ${product.name}`;
      line.append(title, document.createTextNode(` — ${whereText(stock, product)}`));
      root.append(line);
    }
  }
}

function renderWarehouses(stock) {
  const root = document.querySelector("#warehouses");
  const map = balanceMap(stock);
  root.replaceChildren();
  for (const warehouse of stock.warehouses) {
    const card = document.createElement("article");
    card.className = warehouse.status === "archived" ? "warehouse archived" : "warehouse";
    card.dataset.warehouse = String(warehouse.id);
    card.dataset.name = warehouse.name;
    const info = document.createElement("div");
    const header = document.createElement("header");
    const title = document.createElement("strong");
    title.textContent = warehouse.name;
    header.append(title);
    if (warehouse.status === "archived") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "архив";
      header.append(badge);
    }
    info.append(header);
    const lines = stock.products
      .map((product) => {
        const row = readBalance(map, warehouse.id, product.id);
        if (!row.on_hand && !row.reserved_fbs) return "";
        const reserve = row.reserved_fbs ? `, резерв ${row.reserved_fbs}` : "";
        return `${product.name} ${row.on_hand} шт.${reserve}`;
      })
      .filter(Boolean);
    const stockLine = document.createElement("p");
    stockLine.className = "stock-line";
    stockLine.textContent = lines.join(" · ") || "на складе пусто";
    info.append(stockLine);

    const actions = document.createElement("div");
    actions.className = "actions";
    const input = document.createElement("input");
    input.value = warehouse.name;
    input.maxLength = 200;
    input.setAttribute("aria-label", `Новое название склада ${warehouse.name}`);
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "ghost";
    rename.dataset.action = "rename";
    rename.textContent = "Переименовать";
    actions.append(input, rename);
    if (warehouse.status === "active") {
      const archive = document.createElement("button");
      archive.type = "button";
      archive.className = "danger";
      archive.dataset.action = "archive";
      archive.textContent = "Удалить";
      actions.append(archive);
    }
    card.append(info, actions);
    root.append(card);
  }
}

function queueItem(text, buttons) {
  const row = document.createElement("div");
  row.className = "queue-item";
  const label = document.createElement("p");
  label.textContent = text;
  row.append(label);
  const actions = document.createElement("div");
  actions.className = "actions";
  for (const button of buttons) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = button.label;
    element.dataset.action = button.action;
    element.dataset.id = String(button.id);
    if (button.ghost) element.className = "ghost";
    actions.append(element);
  }
  if (buttons.length) row.append(actions);
  return row;
}

function renderQueue(id, items, emptyText) {
  const root = document.getElementById(id);
  root.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = emptyText;
    root.append(empty);
    return;
  }
  for (const item of items) root.append(item);
}

function renderQueues(stock) {
  renderQueue(
    "transfers",
    stock.transfers.map((shipment) => {
      const product = productById(stock, shipment.product_id);
      const text = `${product.sku} ${product.name} · ${shipment.qty} шт. · ${warehouseLabel(warehouseById(stock, shipment.from_warehouse_id))} → ${warehouseLabel(warehouseById(stock, shipment.to_warehouse_id))}`;
      return queueItem(text, [{ action: "receive-transfer", id: shipment.id, label: "Принять на склад" }]);
    }),
    "Нет перемещений в пути",
  );
  renderQueue(
    "reservations",
    stock.reservations.map((reservation) => {
      const product = productById(stock, reservation.product_id);
      const text = `${product.sku} ${product.name} · ${reservation.qty} шт. · ${warehouseLabel(warehouseById(stock, reservation.warehouse_id))}`;
      return queueItem(text, [
        { action: "ship-fbs", id: reservation.id, label: "Отправить клиенту" },
        { action: "cancel-fbs", id: reservation.id, label: "Отменить резерв", ghost: true },
      ]);
    }),
    "Нет активных резервов FBS",
  );
  renderQueue(
    "fbo",
    stock.fbo_shipments.map((shipment) => {
      const product = productById(stock, shipment.product_id);
      const text = `${product.sku} ${product.name} · ${shipment.qty} шт. · ${warehouseLabel(warehouseById(stock, shipment.from_warehouse_id))}`;
      return queueItem(text, [{ action: "receive-fbo", id: shipment.id, label: "Принять на маркетплейс" }]);
    }),
    "Нет поставок FBO в пути",
  );
  renderQueue(
    "fbs",
    stock.fbs_shipments.map((shipment) => {
      const product = productById(stock, shipment.product_id);
      return queueItem(
        `${product.sku} ${product.name} · ${shipment.qty} шт. · ${warehouseLabel(warehouseById(stock, shipment.from_warehouse_id))} → клиент`,
        [],
      );
    }),
    "Нет отправок к клиенту",
  );
}

function formatTime(iso) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(iso));
}

function renderHistory(operations) {
  const body = document.querySelector("#history");
  body.replaceChildren();
  if (!operations.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = "История пуста";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const operation of operations) {
    const row = document.createElement("tr");
    const values = [
      formatTime(operation.created_at),
      operation.type_label,
      operation.sku ? `${operation.sku} ${operation.product_name}` : "—",
      operation.qty ? `${operation.qty} шт.` : "—",
      operation.origin || "—",
      operation.destination || "—",
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    body.append(row);
  }
}

function render() {
  const stock = state.stock;
  if (!stock) return;
  document.querySelector("#store-name").textContent = stock.store ? stock.store.name : "Магазин не создан";
  fillForms(stock);
  renderRecon(stock);
  renderStock(stock);
  renderWhere(stock);
  renderWarehouses(stock);
  renderQueues(stock);
  renderHistory(state.operations);
}

async function refresh() {
  const [stock, history] = await Promise.all([api("GET", "/api/stock"), api("GET", "/api/operations")]);
  state.stock = stock;
  state.operations = history.operations;
  render();
}

function showTab(name) {
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  });
}

function formPayload(form, fields) {
  const payload = {};
  for (const field of fields) {
    if (field === "qty") {
      const qty = integer(form.qty.value);
      if (qty === null) {
        showNotice("bad", noticeHtml("Количество — целое число больше нуля", {}, false));
        return null;
      }
      payload.qty = qty;
    } else if (field === "name") {
      payload.name = form.name.value.trim();
    } else {
      payload[field] = Number(form.elements[field].value);
    }
  }
  return payload;
}

document.querySelector("#form-warehouse").addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = formPayload(event.target, ["name"]);
  if (!payload || !payload.name) {
    showNotice("bad", noticeHtml("Укажите название склада", {}, false));
    return;
  }
  submit("POST", "/api/warehouses", payload);
});

document.querySelector("#form-receipt").addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = formPayload(event.target, ["warehouse_id", "product_id", "qty"]);
  if (payload) submit("POST", "/api/receipts", payload);
});

document.querySelector("#form-transfer").addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = formPayload(event.target, ["from_warehouse_id", "to_warehouse_id", "product_id", "qty"]);
  if (payload) submit("POST", "/api/transfers", payload);
});

document.querySelector("#form-reserve").addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = formPayload(event.target, ["warehouse_id", "product_id", "qty"]);
  if (payload) submit("POST", "/api/fbs/reservations", payload);
});

document.querySelector("#form-fbo").addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = formPayload(event.target, ["warehouse_id", "product_id", "qty"]);
  if (payload) submit("POST", "/api/fbo/shipments", payload);
});

document.body.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    showTab(tab.dataset.tab);
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "replay") {
    replayLast();
    return;
  }
  if (action === "reset") {
    if (!window.confirm("Сбросить все склады, остатки и историю к тестовому приходу из задания?")) return;
    document.body.dataset.busy = "1";
    api("POST", "/api/demo/reset", null, null)
      .then(async (data) => {
        lastRequest = null;
        transferDefaulted = false;
        showNotice("ok", noticeHtml(data.message, null, false));
        await refresh();
      })
      .catch(showFailure)
      .finally(() => {
        document.body.dataset.busy = "";
      });
    return;
  }
  if (action === "rename") {
    const card = button.closest("[data-warehouse]");
    const name = card.querySelector("input").value.trim();
    if (!name) {
      showNotice("bad", noticeHtml("Укажите название склада", {}, false));
      return;
    }
    submit("PATCH", `/api/warehouses/${card.dataset.warehouse}`, { name });
    return;
  }
  if (action === "archive") {
    const card = button.closest("[data-warehouse]");
    if (!window.confirm(`Удалить склад «${card.dataset.name}»? Остатки и история сохранятся, склад останется в архиве.`)) return;
    submit("POST", `/api/warehouses/${card.dataset.warehouse}/archive`);
    return;
  }
  if (action === "receive-transfer") submit("POST", `/api/transfers/${button.dataset.id}/receive`);
  if (action === "receive-fbo") submit("POST", `/api/fbo/shipments/${button.dataset.id}/receive`);
  if (action === "ship-fbs") submit("POST", `/api/fbs/reservations/${button.dataset.id}/ship`);
  if (action === "cancel-fbs") submit("POST", `/api/fbs/reservations/${button.dataset.id}/cancel`);
});

refresh().catch(() => {
  showNotice("bad", noticeHtml("Не удалось загрузить данные. Проверьте, что сервер запущен.", {}, false));
});
