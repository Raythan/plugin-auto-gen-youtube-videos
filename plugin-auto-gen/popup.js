/* global RUN_STORAGE, RUN_STATUS, CHATGPT_URL, WORKFLOW_PHASE, LOOP_STORAGE, LOG_STORAGE, clearUnreadBadge, formatPhaseHint, buildFormBackupPayload, readPopupFormBackup, writePopupFormBackup, applyFormBackup, isFormBackupEmpty, DEFAULT_BRIDGE_URL, DEFAULT_CHANNEL_ID, checkBridgeHealth */

const ext = typeof browser !== "undefined" ? browser : chrome;

const promptInput = document.getElementById("prompt-input");
const flowProjectUrlInput = document.getElementById("flow-project-url-input");
const bridgeUrlInput = document.getElementById("bridge-url-input");
const channelIdInput = document.getElementById("channel-id-input");
const durationInput = document.getElementById("duration-input");
const bridgeStatusEl = document.getElementById("bridge-status");
const roteiroOutput = document.getElementById("roteiro-output");
const titleOutput = document.getElementById("title-output");
const jsonOutput = document.getElementById("json-output");
const imagesGallery = document.getElementById("images-gallery");
const imagePlaceholder = document.getElementById("image-placeholder");
const playBtn = document.getElementById("play-btn");
const loopBtn = document.getElementById("loop-btn");
const cancelBtn = document.getElementById("cancel-btn");
const statusEl = document.getElementById("status");
const metaEl = document.getElementById("meta");
const loopStatusEl = document.getElementById("loop-status");
const loopIndicatorEl = document.getElementById("loop-indicator");
const loopTextEl = document.getElementById("loop-text");
const logListEl = document.getElementById("log-list");
const clearLogBtn = document.getElementById("clear-log-btn");

const STATUS_LABEL = {
  [RUN_STATUS.idle]: "Pronto.",
  [RUN_STATUS.running]: "Executando…",
  [RUN_STATUS.done]: "Fluxo concluído.",
  [RUN_STATUS.error]: "Erro.",
  [RUN_STATUS.cancelled]: "Cancelado.",
};

let loopCheckInterval = null;

function validateBeforeRun() {
  const prompt = promptInput.value.trim();
  const bridgeUrl = bridgeUrlInput.value.trim();
  const channelId = channelIdInput.value.trim();

  if (!prompt) return "Informe o prompt inicial.";
  if (!bridgeUrl) return "Informe a URL do bridge.";
  if (!channelId) return "Informe o ID do canal.";
  return null;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatLogTime(iso) {
  if (!iso) return "--:--:--";
  return new Date(iso).toLocaleTimeString("pt-BR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function renderLogs(entries) {
  logListEl.innerHTML = "";
  if (!entries?.length) {
    const li = document.createElement("li");
    li.className = "log-empty";
    li.textContent = "Nenhum evento registrado ainda.";
    logListEl.appendChild(li);
    return;
  }
  for (const entry of entries) {
    const li = document.createElement("li");
    li.className = `log-item log-${entry.kind || "info"}`;
    li.innerHTML = `<span class="log-time">${formatLogTime(entry.at)}</span><span class="log-msg">${escapeHtml(entry.message)}</span>`;
    logListEl.appendChild(li);
  }
}

function renderImagesGallery(urls) {
  imagesGallery.innerHTML = "";
  const list = Array.isArray(urls) ? urls.filter((u) => u?.startsWith("data:image")) : [];
  if (!list.length) {
    imagePlaceholder.hidden = false;
    return;
  }
  imagePlaceholder.hidden = true;
  list.forEach((url, index) => {
    const img = document.createElement("img");
    img.className = "gallery-thumb";
    img.src = url;
    img.alt = `Imagem ${index + 1}`;
    img.title = `Imagem ${index + 1}`;
    imagesGallery.appendChild(img);
  });
}

async function refreshBridgeStatus() {
  const url = bridgeUrlInput.value.trim() || DEFAULT_BRIDGE_URL;
  const ok = await checkBridgeHealth(url);
  bridgeStatusEl.textContent = ok
    ? "Bridge: online"
    : "Bridge: offline (inicie o console Python)";
  bridgeStatusEl.dataset.state = ok ? "ok" : "error";
}

async function loadLogs() {
  const data = await ext.storage.local.get([LOG_STORAGE.entries]);
  renderLogs(data[LOG_STORAGE.entries] || []);
}

function setStatus(status, extra = "") {
  const base = STATUS_LABEL[status] || status;
  statusEl.textContent = extra ? `${base} ${extra}` : base;
  statusEl.dataset.state = status;
}

function setRunningUi(running, isLoopRunning = false) {
  playBtn.disabled = running || isLoopRunning;
  loopBtn.disabled = running || isLoopRunning;
  loopBtn.hidden = isLoopRunning;
  cancelBtn.hidden = !(running || isLoopRunning);
}

function updateLoopUI(isRunning, iterationCount = 0, nextRunAt = null) {
  loopStatusEl.hidden = false;

  if (isRunning) {
    loopIndicatorEl.classList.add("active");
    const nextTime = nextRunAt ? new Date(nextRunAt).toLocaleTimeString("pt-BR") : "em breve";
    loopTextEl.textContent = `Loop ativo · Ciclo #${iterationCount} · Próximo: ${nextTime}`;
  } else {
    loopIndicatorEl.classList.remove("active");
    loopTextEl.textContent =
      iterationCount > 0
        ? `Loop parado · ${iterationCount} ciclos completos`
        : "Loop parado";
  }
}

function setMeta(updatedAt, phase) {
  const parts = [];
  if (phase === WORKFLOW_PHASE.complete) {
    parts.push("pacote exportado");
  } else if (phase) {
    const hint = formatPhaseHint(phase);
    if (hint) parts.push(hint);
  }
  if (updatedAt) {
    parts.push(new Date(updatedAt).toLocaleString("pt-BR"));
  }
  metaEl.textContent = parts.join(" · ");
}

async function restoreFormBackupIfNeeded(data) {
  const storageBackup = buildFormBackupPayload({
    prompt: data[RUN_STORAGE.prompt],
    bridgeUrl: data[RUN_STORAGE.bridgeUrl],
    channelId: data[RUN_STORAGE.channelId],
    bridgeToken: data[RUN_STORAGE.bridgeToken],
    targetDurationSeconds: data[RUN_STORAGE.targetDurationSeconds],
    flowProjectUrl: data[RUN_STORAGE.flowProjectUrl],
  });
  if (!isFormBackupEmpty(storageBackup)) return storageBackup;

  const popupBackup = readPopupFormBackup();
  if (!isFormBackupEmpty(popupBackup)) {
    await ext.storage.local.set(applyFormBackup(popupBackup));
    return popupBackup;
  }

  try {
    const reply = await sendBackgroundMessage({ action: "GET_FORM_BACKUP" });
    if (reply?.ok && !isFormBackupEmpty(reply.data)) {
      await ext.storage.local.set(applyFormBackup(reply.data));
      writePopupFormBackup(reply.data);
      return reply.data;
    }
  } catch {
    /* background indisponível */
  }

  return null;
}

async function loadState() {
  const data = await ext.storage.local.get([
    RUN_STORAGE.prompt,
    RUN_STORAGE.bridgeUrl,
    RUN_STORAGE.channelId,
    RUN_STORAGE.targetDurationSeconds,
    RUN_STORAGE.flowProjectUrl,
    RUN_STORAGE.roteiroPost,
    RUN_STORAGE.workflowScript,
    RUN_STORAGE.jsonRaw,
    RUN_STORAGE.imagesDataUrls,
    RUN_STORAGE.imageDataUrl,
    RUN_STORAGE.status,
    RUN_STORAGE.error,
    RUN_STORAGE.updatedAt,
    RUN_STORAGE.step,
    RUN_STORAGE.workflowPhase,
    LOOP_STORAGE.isRunning,
    LOOP_STORAGE.iterationCount,
    LOOP_STORAGE.nextRunAt,
  ]);

  const formBackup = await restoreFormBackupIfNeeded(data);

  promptInput.value = formBackup?.prompt || data[RUN_STORAGE.prompt] || "";
  flowProjectUrlInput.value =
    formBackup?.flowProjectUrl || data[RUN_STORAGE.flowProjectUrl] || "";
  bridgeUrlInput.value = formBackup?.bridgeUrl || data[RUN_STORAGE.bridgeUrl] || DEFAULT_BRIDGE_URL;
  channelIdInput.value = formBackup?.channelId || data[RUN_STORAGE.channelId] || DEFAULT_CHANNEL_ID;
  durationInput.value = String(
    formBackup?.targetDurationSeconds || data[RUN_STORAGE.targetDurationSeconds] || 30
  );

  roteiroOutput.value = data[RUN_STORAGE.roteiroPost] || "";
  let title = "";
  try {
    const script = JSON.parse(data[RUN_STORAGE.workflowScript] || "{}");
    title = script.title || "";
  } catch {
    /* ignore */
  }
  titleOutput.value = title;
  jsonOutput.value = data[RUN_STORAGE.jsonRaw] || "";

  const images = data[RUN_STORAGE.imagesDataUrls];
  if (Array.isArray(images) && images.length) {
    renderImagesGallery(images);
  } else if (data[RUN_STORAGE.imageDataUrl]) {
    renderImagesGallery([data[RUN_STORAGE.imageDataUrl]]);
  } else {
    renderImagesGallery([]);
  }

  const status = data[RUN_STORAGE.status] || RUN_STATUS.idle;
  const step = data[RUN_STORAGE.step] || "";
  const error = data[RUN_STORAGE.error] || "";
  const isLoopRunning = !!data[LOOP_STORAGE.isRunning];

  if (status === RUN_STATUS.running) {
    setStatus(status, step || "aguarde nas abas abertas.");
    setRunningUi(true, isLoopRunning);
  } else {
    setStatus(status, error);
    setRunningUi(false, isLoopRunning);
  }

  updateLoopUI(isLoopRunning, data[LOOP_STORAGE.iterationCount] || 0, data[LOOP_STORAGE.nextRunAt]);
  setMeta(data[RUN_STORAGE.updatedAt], data[RUN_STORAGE.workflowPhase]);
  await refreshBridgeStatus();
}

async function sendBackgroundMessage(message) {
  try {
    return await ext.runtime.sendMessage(message);
  } catch {
    throw new Error(
      "Extensão sem background. Remova e carregue de novo em about:debugging."
    );
  }
}

function waitForRunToFinish() {
  return new Promise((resolve) => {
    const listener = (changes, area) => {
      if (area !== "local" || !changes[RUN_STORAGE.status]) return;
      const status = changes[RUN_STORAGE.status].newValue;
      if (status === RUN_STATUS.running) return;
      ext.storage.onChanged.removeListener(listener);
      resolve(status);
    };
    ext.storage.onChanged.addListener(listener);
  });
}

let saveTimer;

function getFormBackupFromUi() {
  return buildFormBackupPayload({
    prompt: promptInput.value,
    bridgeUrl: bridgeUrlInput.value,
    channelId: channelIdInput.value,
    targetDurationSeconds: durationInput.value,
    flowProjectUrl: flowProjectUrlInput.value,
  });
}

function persistFormBackup() {
  const backup = getFormBackupFromUi();
  writePopupFormBackup(backup);
  ext.storage.local.set(applyFormBackup(backup));
  sendBackgroundMessage({ action: "SAVE_FORM_BACKUP", data: backup }).catch(() => {});
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(persistFormBackup, 300);
}

function flushFormBackup() {
  clearTimeout(saveTimer);
  persistFormBackup();
}

function buildStartPayload() {
  return {
    prompt: promptInput.value.trim(),
    bridgeUrl: bridgeUrlInput.value.trim() || DEFAULT_BRIDGE_URL,
    channelId: channelIdInput.value.trim() || DEFAULT_CHANNEL_ID,
    targetDurationSeconds: Number(durationInput.value) || 30,
    flowProjectUrl: flowProjectUrlInput.value.trim(),
  };
}

promptInput.addEventListener("input", scheduleSave);
flowProjectUrlInput.addEventListener("input", scheduleSave);
bridgeUrlInput.addEventListener("input", () => {
  scheduleSave();
  refreshBridgeStatus();
});
channelIdInput.addEventListener("input", scheduleSave);
durationInput.addEventListener("input", scheduleSave);
window.addEventListener("pagehide", flushFormBackup);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flushFormBackup();
});

playBtn.addEventListener("click", async () => {
  const validationError = validateBeforeRun();
  if (validationError) {
    setStatus(RUN_STATUS.error, validationError);
    return;
  }

  const payload = buildStartPayload();

  setRunningUi(true, false);
  setStatus(RUN_STATUS.running, "Abrindo ChatGPT (roteiro)…");

  try {
    const tab = await ext.tabs.create({ url: CHATGPT_URL, active: true });

    await ext.storage.local.set({
      ...applyFormBackup(getFormBackupFromUi()),
      [RUN_STORAGE.cancelRequested]: false,
      [RUN_STORAGE.status]: RUN_STATUS.running,
      [RUN_STORAGE.error]: "",
      [RUN_STORAGE.step]: "Abrindo ChatGPT (roteiro)…",
      [RUN_STORAGE.activeTabId]: tab.id,
      [RUN_STORAGE.workflowPhase]: WORKFLOW_PHASE.json,
    });

    const reply = await sendBackgroundMessage({
      action: "START_WORKFLOW",
      ...payload,
      tabId: tab.id,
    });

    if (!reply?.ok) {
      throw new Error(reply?.error || "Falha ao iniciar.");
    }

    const finalStatus = await waitForRunToFinish();
    await loadState();

    if (finalStatus === RUN_STATUS.error) {
      const data = await ext.storage.local.get(RUN_STORAGE.error);
      throw new Error(data[RUN_STORAGE.error] || "Erro no fluxo.");
    }
  } catch (err) {
    const msg = err?.message || String(err);
    setStatus(RUN_STATUS.error, msg);
    await ext.storage.local.set({
      [RUN_STORAGE.status]: RUN_STATUS.error,
      [RUN_STORAGE.error]: msg,
    });
    setRunningUi(false, false);
    await loadState();
  } finally {
    const data = await ext.storage.local.get(RUN_STORAGE.status);
    if (data[RUN_STORAGE.status] !== RUN_STATUS.running) {
      const loopData = await ext.storage.local.get(LOOP_STORAGE.isRunning);
      setRunningUi(false, !!loopData[LOOP_STORAGE.isRunning]);
    }
  }
});

loopBtn.addEventListener("click", async () => {
  const validationError = validateBeforeRun();
  if (validationError) {
    setStatus(RUN_STATUS.error, validationError);
    return;
  }

  const payload = buildStartPayload();

  setRunningUi(false, true);
  setStatus(RUN_STATUS.running, "Iniciando loop infinito…");
  updateLoopUI(true, 0, new Date(Date.now() + 120 * 60000).toISOString());

  try {
    const reply = await sendBackgroundMessage({
      action: "START_LOOP",
      ...payload,
    });

    if (!reply?.ok) {
      throw new Error(reply?.error || "Falha ao iniciar loop.");
    }

    startLoopStatusPolling();
  } catch (err) {
    const msg = err?.message || String(err);
    setStatus(RUN_STATUS.error, msg);
    updateLoopUI(false, 0);
    setRunningUi(false, false);
  }
});

cancelBtn.addEventListener("click", async () => {
  const loopData = await ext.storage.local.get(LOOP_STORAGE.isRunning);
  const isLoopRunning = !!loopData[LOOP_STORAGE.isRunning];

  setStatus(RUN_STATUS.cancelled, isLoopRunning ? "Parando loop…" : "Parando…");

  try {
    if (isLoopRunning) {
      await sendBackgroundMessage({ action: "STOP_LOOP" });
      stopLoopStatusPolling();
    } else {
      await sendBackgroundMessage({ action: "CANCEL_RUN" });
    }
  } catch {
    await ext.storage.local.set({
      [RUN_STORAGE.cancelRequested]: true,
      [RUN_STORAGE.status]: RUN_STATUS.cancelled,
    });
  }

  setRunningUi(false, false);
  updateLoopUI(false, 0);
  await loadState();
});

function startLoopStatusPolling() {
  stopLoopStatusPolling();
  loopCheckInterval = setInterval(async () => {
    try {
      const reply = await sendBackgroundMessage({ action: "GET_LOOP_STATUS" });
      if (reply?.ok) {
        updateLoopUI(reply.isRunning, reply.iterationCount, reply.nextRunAt);
        if (!reply.isRunning) {
          stopLoopStatusPolling();
          setRunningUi(false, false);
        }
      }
    } catch {
      /* ignora */
    }
  }, 10000);
}

function stopLoopStatusPolling() {
  if (loopCheckInterval) {
    clearInterval(loopCheckInterval);
    loopCheckInterval = null;
  }
}

clearLogBtn.addEventListener("click", async () => {
  await ext.storage.local.set({ [LOG_STORAGE.entries]: [] });
  renderLogs([]);
});

ext.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  const keys = Object.values(RUN_STORAGE);
  const loopKeys = Object.values(LOOP_STORAGE);
  if (keys.some((k) => changes[k]) || loopKeys.some((k) => changes[k])) {
    loadState();
  }
  if (changes[LOG_STORAGE.entries]) {
    renderLogs(changes[LOG_STORAGE.entries].newValue || []);
  }
});

async function onPopupOpen() {
  try {
    await sendBackgroundMessage({ action: "POPUP_OPENED" });
  } catch {
    await clearUnreadBadge(ext);
  }
  await loadLogs();
}

loadState().then(async () => {
  await onPopupOpen();

  const data = await ext.storage.local.get([
    RUN_STORAGE.status,
    LOOP_STORAGE.isRunning,
  ]);

  const isRunning = data[RUN_STORAGE.status] === RUN_STATUS.running;
  const isLoopRunning = !!data[LOOP_STORAGE.isRunning];

  setRunningUi(isRunning, isLoopRunning);

  if (isLoopRunning) {
    startLoopStatusPolling();
  }
});
