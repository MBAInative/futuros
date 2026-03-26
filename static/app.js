const recommendationsBody = document.getElementById("recommendationsBody");
const positionsBody = document.getElementById("positionsBody");
const statusValue = document.getElementById("statusValue");
const statusMessage = document.getElementById("statusMessage");
const lastRefresh = document.getElementById("lastRefresh");
const refreshHint = document.getElementById("refreshHint");
const openPnlMetric = document.getElementById("openPnlMetric");
const realizedPnlMetric = document.getElementById("realizedPnlMetric");
const refreshButton = document.getElementById("refreshButton");
const resetButton = document.getElementById("resetButton");
const downloadButton = document.getElementById("downloadButton");
const autoBuyThreshold = document.getElementById("autoBuyThreshold");
const autoBuyThresholdValue = document.getElementById("autoBuyThresholdValue");
const helpButton = document.getElementById("helpButton");
const helpDialog = document.getElementById("helpDialog");
const closeHelp = document.getElementById("closeHelp");

function formatMoney(value) {
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value || 0);
}

function formatPct(value) {
  return `${((value || 0) * 100).toFixed(2)}%`;
}

function formatDate(isoValue) {
  if (!isoValue) return "-";
  return new Date(isoValue).toLocaleString("es-ES");
}

function pnlClass(value) {
  if (value > 0) return "pnl-positive";
  if (value < 0) return "pnl-negative";
  return "pnl-flat";
}

function familyLabel(family) {
  return family || "sin grupo";
}

function familyClass(family) {
  return `family-chip family-${family || "otro"}`;
}

function instrumentCell(item, universe) {
  const family = universe[item.ticker]?.family || "otro";
  return `<strong>${item.ticker}</strong><br><span class="muted">${item.name}</span><br><span class="${familyClass(family)}">${familyLabel(family)}</span>`;
}

function renderRecommendations(items, universe) {
  recommendationsBody.innerHTML = "";
  if (!items.length) {
    recommendationsBody.innerHTML = `<tr><td colspan="7">Todavia no hay senales calculadas.</td></tr>`;
    return;
  }

  for (const item of items) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${instrumentCell(item, universe)}</td>
      <td><span class="pill ${item.action}">${item.action}</span></td>
      <td>${item.strength.toFixed(2)}</td>
      <td>${item.price.toFixed(3)}</td>
      <td>${item.volume_z.toFixed(2)}</td>
      <td>${formatPct(item.return_5m)}</td>
      <td>${item.reason}</td>
    `;
    recommendationsBody.appendChild(row);
  }
}

function renderPositions(items, universe) {
  positionsBody.innerHTML = "";
  if (!items.length) {
    positionsBody.innerHTML = `<tr><td colspan="7">Sin operaciones simuladas todavia.</td></tr>`;
    openPnlMetric.innerHTML = `PnL abierto: <span class="pnl-flat">${formatMoney(0)}</span>`;
    realizedPnlMetric.innerHTML = `PnL realizado: <span class="pnl-flat">${formatMoney(0)}</span>`;
    return;
  }

  let openPnl = 0;
  let realized = 0;
  for (const item of items) {
    openPnl += item.status === "open" ? item.unrealized_pnl : 0;
    realized += item.status === "closed" ? item.realized_pnl : 0;
    const pnl = item.status === "open" ? item.unrealized_pnl : item.realized_pnl;
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${instrumentCell(item, universe)}</td>
      <td><span class="pill ${item.side}">${item.side}</span></td>
      <td>${formatMoney(item.nominal_usd)}</td>
      <td>${item.entry_price.toFixed(3)}</td>
      <td>${item.current_price.toFixed(3)}</td>
      <td><span class="${pnlClass(pnl)}">${formatMoney(pnl)}</span></td>
      <td>${item.status}</td>
    `;
    positionsBody.appendChild(row);
  }

  openPnlMetric.innerHTML = `PnL abierto: <span class="${pnlClass(openPnl)}">${formatMoney(openPnl)}</span>`;
  realizedPnlMetric.innerHTML = `PnL realizado: <span class="${pnlClass(realized)}">${formatMoney(realized)}</span>`;
}

function syncThreshold(value) {
  autoBuyThreshold.value = value.toFixed(1);
  autoBuyThresholdValue.textContent = value.toFixed(1);
}

async function loadState() {
  const response = await fetch("/api/state");
  const data = await response.json();
  statusValue.textContent = data.runtime.status;
  statusMessage.textContent = data.runtime.message;
  lastRefresh.textContent = formatDate(data.runtime.last_refresh);
  refreshHint.textContent = `Refresco automatico: cada ${data.config.refresh_seconds} segundos`;
  syncThreshold(Number(data.config.auto_buy_threshold || 7));
  renderRecommendations(data.recommendations, data.config.universe);
  renderPositions(data.positions, data.config.universe);
}

async function updateThreshold() {
  const value = Number(autoBuyThreshold.value);
  autoBuyThresholdValue.textContent = value.toFixed(1);
  const response = await fetch(`/api/settings/auto-buy-threshold/${value}`, { method: "POST" });
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.detail || "No se pudo actualizar el umbral");
  }
  const data = await response.json();
  syncThreshold(Number(data.auto_buy_threshold));
  await loadState();
}

async function triggerRefresh() {
  refreshButton.disabled = true;
  refreshButton.textContent = "Actualizando...";
  try {
    const response = await fetch("/api/refresh", { method: "POST" });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || "No se pudo actualizar");
    }
    await loadState();
  } catch (error) {
    statusValue.textContent = "error";
    statusMessage.textContent = error.message;
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "Actualizar ahora";
  }
}

async function triggerReset() {
  if (!window.confirm("Esto borrara recomendaciones, operaciones y equity de prueba. ¿Quieres continuar?")) {
    return;
  }
  resetButton.disabled = true;
  try {
    const response = await fetch("/api/reset", { method: "POST" });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || "No se pudo reiniciar");
    }
    await loadState();
  } catch (error) {
    statusValue.textContent = "error";
    statusMessage.textContent = error.message;
  } finally {
    resetButton.disabled = false;
  }
}

function triggerDownload() {
  window.location.href = "/api/export.xlsx";
}

autoBuyThreshold.addEventListener("input", () => {
  autoBuyThresholdValue.textContent = Number(autoBuyThreshold.value).toFixed(1);
});

autoBuyThreshold.addEventListener("change", async () => {
  try {
    await updateThreshold();
  } catch (error) {
    statusValue.textContent = "error";
    statusMessage.textContent = error.message;
  }
});

refreshButton.addEventListener("click", triggerRefresh);
resetButton.addEventListener("click", triggerReset);
downloadButton.addEventListener("click", triggerDownload);
helpButton.addEventListener("click", () => helpDialog.showModal());
closeHelp.addEventListener("click", () => helpDialog.close());

loadState();
setInterval(loadState, 10000);
