/* global RUN_STORAGE, RUN_STATUS, WORKFLOW_PHASE, CHATGPT_URL, LABS_FLOW_ENTRY_URL, isLabsFlowUrl, isLabsFlowTabUrl, parseWorkflowJson, computeSceneCount, buildJsonInstruction, LOOP_STORAGE, LOOP_CONFIG, WORKFLOW_TAB_STORAGE, ENGINE_STORAGE, ENGINE_PHASE, ENGINE_ALARMS, STEP_MAX_ATTEMPTS, STEP_TIMEOUT_BY_PHASE, STEP_RETRY_DELAY_MS, logStageStart, logWorkflowStage, appendWorkflowLog, addRecentJsonResponse, getRecentJsonResponses, buildJsonVariationPrompt, clearUnreadBadge, formatStepProgress, exportContentPackage, isFormBackupEmpty, DEFAULT_BRIDGE_URL, DEFAULT_CHANNEL_ID */

const ext = typeof browser !== "undefined" ? browser : chrome;
const CHATGPT_HOSTS = ["chatgpt.com", "chat.openai.com"];
const LABS_HOSTS = ["labs.google"];

let activeRun = { cancelled: false, chatTabId: null, labsTabId: null };
let loopState = { isRunning: false };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CHATGPT_URL_PATTERNS = ["https://chatgpt.com/*", "https://chat.openai.com/*"];
const LABS_URL_PATTERNS = ["https://labs.google/*"];
const LABS_CONTENT_SCRIPTS = [
  "shared/constants.js",
  "shared/image-capture.js",
  "content/labs-flow.js",
];
const LABS_PAGE_BRIDGE = ["shared/flow-composer-dom.js", "content/labs-flow-page.js"];

async function saveFormBackupToChatGpt(data) {
  const tabs = await ext.tabs.query({ url: CHATGPT_URL_PATTERNS });
  for (const tab of tabs) {
    try {
      await ext.tabs.sendMessage(tab.id, { action: "SAVE_FORM_BACKUP", data });
    } catch {
      /* aba sem content script */
    }
  }
}

async function loadFormBackupFromChatGpt() {
  const tabs = await ext.tabs.query({ url: CHATGPT_URL_PATTERNS });
  for (const tab of tabs) {
    try {
      const reply = await ext.tabs.sendMessage(tab.id, { action: "GET_FORM_BACKUP" });
      if (reply?.data && !isFormBackupEmpty(reply.data)) return reply.data;
    } catch {
      /* ignora */
    }
  }
  return null;
}

async function setRunState(partial) {
  await ext.storage.local.set({
    ...partial,
    [RUN_STORAGE.updatedAt]: new Date().toISOString(),
  });
}

async function setStep(step) {
  await setRunState({ [RUN_STORAGE.step]: step });
}

async function logAndSetWorkflowStep(phaseId, detail) {
  await logWorkflowStage(ext, phaseId, detail);
  const progress = formatStepProgress(phaseId);
  await setStep(`${progress} — ${detail}`);
}

function isChatGptUrl(url) {
  if (!url) return false;
  try {
    const host = new URL(url).hostname.toLowerCase();
    return CHATGPT_HOSTS.some((h) => host === h || host.endsWith(`.${h}`) || host.includes(h));
  } catch {
    return /chatgpt\.com|chat\.openai\.com/i.test(url);
  }
}

function waitForFlowProjectUrl(tabId, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      ext.tabs.onUpdated.removeListener(listener);
      reject(new Error("Tempo esgotado aguardando projeto Google Flow."));
    }, timeoutMs);

    async function checkUrl(url) {
      if (!url || !isLabsFlowUrl(url)) return false;
      await ext.storage.local.set({ [RUN_STORAGE.flowProjectUrl]: url.split("#")[0] });
      return true;
    }

    async function listener(updatedTabId, info, tab) {
      if (updatedTabId !== tabId) return;
      const url = tab?.url || info?.url;
      if (url && (await checkUrl(url))) {
        clearTimeout(timeout);
        ext.tabs.onUpdated.removeListener(listener);
        resolve(url);
      }
    }

    ext.tabs.onUpdated.addListener(listener);
    ext.tabs.get(tabId).then(async (tab) => {
      if (tab?.url && (await checkUrl(tab.url))) {
        clearTimeout(timeout);
        ext.tabs.onUpdated.removeListener(listener);
        resolve(tab.url);
      }
    });
  });
}

async function prepareLabsFlowTab() {
  let tabId = activeRun.labsTabId;
  if (tabId != null) {
    try {
      const tab = await ext.tabs.get(tabId);
      if (tab.url && isLabsFlowTabUrl(tab.url)) {
        await activateWorkflowTab(tabId);
        activeRun.labsTabId = tabId;
        await persistWorkflowTabIds();
        if (!isLabsFlowUrl(tab.url)) {
          await waitForFlowProjectUrl(tabId);
        }
        await ensureContentScript(tabId, LABS_CONTENT_SCRIPTS);
        return tabId;
      }
      tabId = null;
    } catch {
      tabId = null;
    }
  }

  const stored = await ext.storage.local.get([RUN_STORAGE.flowProjectUrl]);
  const savedProjectUrl = (stored[RUN_STORAGE.flowProjectUrl] || "").trim();
  const tabs = await ext.tabs.query({});
  const existingFlow = tabs.find((t) => t.url && isLabsFlowUrl(t.url));

  if (existingFlow?.id != null) {
    tabId = existingFlow.id;
    const targetUrl = savedProjectUrl || existingFlow.url;
    await ext.tabs.update(tabId, { active: true, url: targetUrl });
  } else if (savedProjectUrl) {
    const created = await ext.tabs.create({ url: savedProjectUrl, active: true });
    tabId = created.id;
  } else {
    const created = await ext.tabs.create({ url: LABS_FLOW_ENTRY_URL, active: true });
    tabId = created.id;
  }

  activeRun.labsTabId = tabId;
  await persistWorkflowTabIds();
  await waitForTabComplete(tabId);

  try {
    const tab = await ext.tabs.get(tabId);
    if (!isLabsFlowUrl(tab.url)) {
      await waitForFlowProjectUrl(tabId);
    } else {
      await ext.storage.local.set({
        [RUN_STORAGE.flowProjectUrl]: tab.url.split("#")[0],
      });
    }
  } catch (err) {
    if (!savedProjectUrl) throw err;
  }

  await sleep(2000);
  await ensureContentScript(tabId, LABS_CONTENT_SCRIPTS);
  await activateWorkflowTab(tabId);
  return tabId;
}

function isCancelled() {
  return activeRun.cancelled;
}

async function fireTabMessage(tabId, payload, { required = false } = {}) {
  await activateWorkflowTab(tabId);
  const files =
    payload?.action === "RUN_IMAGE"
      ? LABS_CONTENT_SCRIPTS
      : ["shared/constants.js", "shared/parse-json.js", "content/chatgpt.js"];

  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      await ext.tabs.sendMessage(tabId, payload);
      return;
    } catch {
      try {
        await ensureContentScript(tabId, files);
        await ext.tabs.sendMessage(tabId, payload);
        return;
      } catch (err) {
        if (attempt === 7 && required) {
          throw new Error(
            `Não foi possível enviar comando à aba (${payload?.action || "message"}). Recarregue a página.`
          );
        }
      }
    }
    await sleep(500);
  }
}

async function readCancelFlag() {
  const data = await ext.storage.local.get(RUN_STORAGE.cancelRequested);
  return !!data[RUN_STORAGE.cancelRequested];
}

async function throwIfCancelled() {
  if (isCancelled() || (await readCancelFlag())) {
    throw new Error("Execução cancelada pelo usuário.");
  }
}

function waitForTabComplete(tabId, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      ext.tabs.onUpdated.removeListener(listener);
      reject(new Error("Tempo esgotado ao carregar a aba."));
    }, timeoutMs);

    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") {
        clearTimeout(timeout);
        ext.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }

    ext.tabs.onUpdated.addListener(listener);
    ext.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") {
        clearTimeout(timeout);
        ext.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    });
  });
}

async function activateWorkflowTab(tabId) {
  if (tabId == null) return;
  try {
    const tab = await ext.tabs.get(tabId);
    if (tab.windowId != null) {
      try {
        const win = await ext.windows.get(tab.windowId);
        if (win.state === "minimized") {
          await ext.windows.update(tab.windowId, { state: "normal", focused: false });
          await sleep(400);
        }
      } catch {
        /* ignore */
      }
    }
    await ext.tabs.update(tabId, { active: true });
    await sleep(200);
  } catch {
    /* aba fechada */
  }
}

async function ensureLabsPageBridge(tabId) {
  try {
    await ext.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      files: LABS_PAGE_BRIDGE,
    });
  } catch {
    /* manifest ou já injetado */
  }
}

async function runFlowPageBridge(tabId, bridgeAction, payload = {}) {
  if (!tabId) return { ok: false, reason: "no-tab" };
  await activateWorkflowTab(tabId);
  await ensureLabsPageBridge(tabId);
  await sleep(150);
  try {
    const results = await ext.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (action, data) => {
        if (action === "fillPrompt" && typeof globalThis.__pluginAutoGenFlowFillPrompt === "function") {
          return globalThis.__pluginAutoGenFlowFillPrompt(data.prompt || "");
        }
        if (action === "clickSubmit" && typeof globalThis.__pluginAutoGenFlowClickSubmit === "function") {
          return globalThis.__pluginAutoGenFlowClickSubmit();
        }
        const SUBMIT = "plugin-auto-gen-flow-submit";
        const RESULT = "plugin-auto-gen-flow-submit-result";
        return new Promise((resolve) => {
          const onResult = (event) => {
            document.removeEventListener(RESULT, onResult);
            resolve(event.detail || { ok: false, reason: "empty-result" });
          };
          document.addEventListener(RESULT, onResult);
          document.dispatchEvent(
            new CustomEvent(SUBMIT, { bubbles: true, composed: true })
          );
          setTimeout(() => {
            document.removeEventListener(RESULT, onResult);
            resolve({ ok: false, reason: "page-bridge-timeout" });
          }, 8000);
        });
      },
      args: [bridgeAction, payload],
    });
    return results?.[0]?.result ?? { ok: false, reason: "no-result" };
  } catch (err) {
    return { ok: false, reason: err?.message || String(err), mainWorldFailed: true };
  }
}

async function clickFlowSubmitInPage(tabId) {
  return runFlowPageBridge(tabId, "clickSubmit");
}

async function ensureContentScript(tabId, files) {
  const isLabs = files === LABS_CONTENT_SCRIPTS;
  await activateWorkflowTab(tabId);
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await throwIfCancelled();
    try {
      const pong = await ext.tabs.sendMessage(tabId, { action: "PING" });
      if (pong?.ok) {
        if (isLabs) await ensureLabsPageBridge(tabId);
        return;
      }
    } catch {
      /* injeta */
    }
    try {
      await ext.scripting.executeScript({ target: { tabId }, files });
      if (isLabs) await ensureLabsPageBridge(tabId);
    } catch {
      /* já registrado */
    }
    await sleep(500);
  }
  throw new Error("Não foi possível conectar à página. Recarregue a aba e tente novamente.");
}

async function persistWorkflowTabIds() {
  await ext.storage.local.set({
    [WORKFLOW_TAB_STORAGE.chatTabId]: activeRun.chatTabId ?? null,
    [WORKFLOW_TAB_STORAGE.labsTabId]: activeRun.labsTabId ?? null,
  });
}

async function loadWorkflowTabIds() {
  const stored = await ext.storage.local.get([
    WORKFLOW_TAB_STORAGE.chatTabId,
    WORKFLOW_TAB_STORAGE.labsTabId,
  ]);
  if (stored[WORKFLOW_TAB_STORAGE.chatTabId] != null) {
    activeRun.chatTabId = stored[WORKFLOW_TAB_STORAGE.chatTabId];
  }
  if (stored[WORKFLOW_TAB_STORAGE.labsTabId] != null) {
    activeRun.labsTabId = stored[WORKFLOW_TAB_STORAGE.labsTabId];
  }
}

async function prepareChatGptTab() {
  let tabId = activeRun.chatTabId;
  let reuseExisting = false;

  if (tabId != null) {
    try {
      await ext.tabs.get(tabId);
      reuseExisting = true;
    } catch {
      tabId = null;
    }
  }

  if (tabId == null) {
    const tabs = await ext.tabs.query({});
    const existing = tabs.find((t) => t.url && isChatGptUrl(t.url));
    if (existing?.id != null) {
      tabId = existing.id;
      await ext.tabs.update(tabId, { active: true, url: CHATGPT_URL });
    } else {
      const created = await ext.tabs.create({ url: CHATGPT_URL, active: true });
      tabId = created.id;
    }
  } else if (reuseExisting) {
    await ext.tabs.update(tabId, { active: true });
  }

  activeRun.chatTabId = tabId;
  await persistWorkflowTabIds();
  await waitForTabComplete(tabId);
  await sleep(reuseExisting ? 800 : 1500);
  await ensureContentScript(tabId, [
    "shared/constants.js",
    "shared/parse-json.js",
    "content/chatgpt.js",
  ]);
  await activateWorkflowTab(tabId);
  return tabId;
}

async function closeChatgptTab() {
  await loadWorkflowTabIds();
  if (activeRun.chatTabId != null) {
    try {
      await ext.tabs.remove(activeRun.chatTabId);
    } catch {
      /* ignore */
    }
    activeRun.chatTabId = null;
  }

  const allTabs = await ext.tabs.query({});
  for (const tab of allTabs) {
    if (tab.id == null || !tab.url || !isChatGptUrl(tab.url)) continue;
    try {
      await ext.tabs.remove(tab.id);
    } catch {
      /* ignore */
    }
  }

  await ext.storage.local.remove([WORKFLOW_TAB_STORAGE.chatTabId]);
}

async function closeLabsTab() {
  await loadWorkflowTabIds();
  if (activeRun.labsTabId != null) {
    try {
      await ext.tabs.remove(activeRun.labsTabId);
    } catch {
      /* ignore */
    }
    activeRun.labsTabId = null;
  }

  const allTabs = await ext.tabs.query({});
  for (const tab of allTabs) {
    if (tab.id == null || !tab.url || !isLabsFlowTabUrl(tab.url)) continue;
    try {
      await ext.tabs.remove(tab.id);
    } catch {
      /* ignore */
    }
  }

  await ext.storage.local.remove([WORKFLOW_TAB_STORAGE.labsTabId]);
}

async function closeWorkflowTabs() {
  await closeLabsTab();
  await closeChatgptTab();
  await setStep("Abas ChatGPT e Google Flow fechadas.");
}

async function scheduleTabCleanup() {
  await persistWorkflowTabIds();
  await ext.alarms.clear(LOOP_CONFIG.closeChatgptAlarmName);
  await ext.alarms.create(LOOP_CONFIG.closeChatgptAlarmName, {
    when: Date.now() + LOOP_CONFIG.closeChatgptDelaySeconds * 1000,
  });
  await setEnginePhase(ENGINE_PHASE.closingChatgpt);
  await logStageStart(ext, "close_chatgpt", `em ${LOOP_CONFIG.closeChatgptDelaySeconds}s`);
  await setStep(`Pacote exportado. Abas fecham em ${LOOP_CONFIG.closeChatgptDelaySeconds}s…`);
  await startKeepalive();
}

async function onCloseChatgptAlarm() {
  await logStageStart(ext, "close_chatgpt");
  await closeWorkflowTabs();
  await finishCloseTabsAndScheduleNext();
}

async function cleanupTemporaryData() {
  const loopData = await ext.storage.local.get(LOOP_STORAGE.isRunning);
  const isLoop = !!loopData[LOOP_STORAGE.isRunning];
  const keysToRemove = [
    RUN_STORAGE.response,
    RUN_STORAGE.roteiroPost,
    RUN_STORAGE.promptImagem,
    RUN_STORAGE.jsonRaw,
    RUN_STORAGE.imageDataUrl,
    RUN_STORAGE.imagesDataUrls,
    RUN_STORAGE.imageIndex,
    RUN_STORAGE.imageTotal,
    RUN_STORAGE.workflowScript,
    RUN_STORAGE.workflowPhase,
    RUN_STORAGE.activeTabId,
    RUN_STORAGE.error,
    RUN_STORAGE.updatedAt,
    ENGINE_STORAGE.stepResult,
  ];
  if (!isLoop) {
    keysToRemove.push(RUN_STORAGE.step, RUN_STORAGE.status);
  }
  await ext.storage.local.remove(keysToRemove);
}

const KEEPALIVE_MS = 3000;

async function startKeepalive() {
  await ext.alarms.clear(ENGINE_ALARMS.keepalive);
  await ext.alarms.create(ENGINE_ALARMS.keepalive, { when: Date.now() + KEEPALIVE_MS });
}

async function rescheduleKeepalive() {
  const phase = await getEnginePhase();
  if (phase === ENGINE_PHASE.idle) return;
  await ext.alarms.clear(ENGINE_ALARMS.keepalive);
  await ext.alarms.create(ENGINE_ALARMS.keepalive, { when: Date.now() + KEEPALIVE_MS });
}

async function stopKeepalive() {
  await ext.alarms.clear(ENGINE_ALARMS.keepalive);
}

function scheduleEngineStep(delayMs = 1500) {
  ext.alarms.clear(ENGINE_ALARMS.step).then(() => {
    ext.alarms.create(ENGINE_ALARMS.step, { when: Date.now() + delayMs });
  });
}

async function setEnginePhase(phase) {
  await ext.storage.local.set({ [ENGINE_STORAGE.phase]: phase });
}

async function getEnginePhase() {
  const data = await ext.storage.local.get(ENGINE_STORAGE.phase);
  return data[ENGINE_STORAGE.phase] || ENGINE_PHASE.idle;
}

function stepNameToPhase(step) {
  if (step === "RUN_TEXT") return ENGINE_PHASE.json;
  if (step === "RUN_IMAGE") return ENGINE_PHASE.image;
  return null;
}

function phaseLabel(phase) {
  if (phase === ENGINE_PHASE.json) return "JSON";
  if (phase === ENGINE_PHASE.image) return "imagem";
  if (phase === ENGINE_PHASE.export) return "exportação";
  return phase;
}

async function isEngineLoop() {
  const data = await ext.storage.local.get([ENGINE_STORAGE.isLoop, LOOP_STORAGE.isRunning]);
  return !!data[ENGINE_STORAGE.isLoop] || !!data[LOOP_STORAGE.isRunning];
}

async function getStepAttempt() {
  const data = await ext.storage.local.get(ENGINE_STORAGE.stepAttempt);
  const attempt = Number(data[ENGINE_STORAGE.stepAttempt]);
  return attempt >= 1 ? attempt : 1;
}

async function setStepAttempt(attempt) {
  await ext.storage.local.set({ [ENGINE_STORAGE.stepAttempt]: attempt });
}

async function resetStepAttempt() {
  await ext.storage.local.set({ [ENGINE_STORAGE.stepAttempt]: 1 });
  await ext.storage.local.remove(ENGINE_STORAGE.stepStartedAt);
}

async function markStepAttemptStarted() {
  await ext.storage.local.set({ [ENGINE_STORAGE.stepStartedAt]: Date.now() });
}

async function handleStepFailure(errorMsg, phase = null) {
  if (isCancelled() || (await readCancelFlag())) return;

  await ext.storage.local.remove(ENGINE_STORAGE.stepStartedAt);

  const currentPhase = phase || (await getEnginePhase());
  const attempt = await getStepAttempt();
  const label = phaseLabel(currentPhase);

  await appendWorkflowLog(ext, {
    kind: "error",
    message: `Tentativa ${attempt}/${STEP_MAX_ATTEMPTS} falhou (${label}): ${errorMsg}`,
    stage: currentPhase || "error",
  });

  await ext.storage.local.remove(ENGINE_STORAGE.stepResult);

  if (attempt < STEP_MAX_ATTEMPTS) {
    const nextAttempt = attempt + 1;
    await setStepAttempt(nextAttempt);
    await setRunState({
      [RUN_STORAGE.status]: RUN_STATUS.running,
      [RUN_STORAGE.error]: errorMsg,
      [RUN_STORAGE.step]: `${label}: tentativa ${attempt} falhou. Repetindo (${nextAttempt}/${STEP_MAX_ATTEMPTS})…`,
    });
    scheduleEngineStep(STEP_RETRY_DELAY_MS);
    return;
  }

  await appendWorkflowLog(ext, {
    kind: "error",
    message: `Etapa ${label} encerrada após ${STEP_MAX_ATTEMPTS} tentativas: ${errorMsg}`,
    stage: "error",
  });

  await recoverAfterStepFailure(errorMsg);
}

function validateContentConfig(prompt, bridgeUrl, channelId) {
  if (!prompt) return "Informe o prompt inicial.";
  if (!bridgeUrl) return "Informe a URL do bridge.";
  if (!channelId) return "Informe o ID do canal.";
  return null;
}

async function finalizeExport(isLoop, exportResult) {
  await setRunState({
    [RUN_STORAGE.status]: isLoop ? RUN_STATUS.running : RUN_STATUS.done,
    [RUN_STORAGE.workflowPhase]: WORKFLOW_PHASE.complete,
    [RUN_STORAGE.step]: isLoop
      ? `Loop: pacote exportado (${exportResult?.id || "ok"})! Fechando ChatGPT…`
      : `Pacote exportado (${exportResult?.id || "ok"})!`,
  });

  await logStageStart(ext, "export", exportResult?.id || "sucesso");
  await cleanupTemporaryData();
  await scheduleTabCleanup();
}

async function scheduleNextLoopCycle({ incrementCount = true, failureMessage = "" } = {}) {
  const loopData = await ext.storage.local.get([
    LOOP_STORAGE.isRunning,
    LOOP_STORAGE.iterationCount,
  ]);
  if (!loopData[LOOP_STORAGE.isRunning]) return false;

  const count = incrementCount
    ? (loopData[LOOP_STORAGE.iterationCount] || 0) + 1
    : loopData[LOOP_STORAGE.iterationCount] || 0;

  await ext.storage.local.set({
    [LOOP_STORAGE.iterationCount]: count,
    [LOOP_STORAGE.lastRunAt]: new Date().toISOString(),
    [LOOP_STORAGE.nextRunAt]: new Date(
      Date.now() + LOOP_CONFIG.intervalMinutes * 60000
    ).toISOString(),
  });

  await ext.alarms.clear(LOOP_CONFIG.alarmName);
  await ext.alarms.create(LOOP_CONFIG.alarmName, {
    delayInMinutes: LOOP_CONFIG.intervalMinutes,
  });

  await setEnginePhase(ENGINE_PHASE.idle);
  await stopKeepalive();

  if (failureMessage) {
    await logStageStart(ext, "cycle_done", "falha — próximo em 2h");
    await setStep(`Loop: falha (${failureMessage}). Próximo ciclo em 2h.`);
  } else {
    await logStageStart(ext, "cycle_done", `ciclo #${count} — próximo em 2h`);
    await setStep(`Loop: ciclo #${count} concluído. Próximo em 2h.`);
  }

  return true;
}

async function recoverAfterStepFailure(errorMsg) {
  const isLoop = await isEngineLoop();

  await resetStepAttempt();
  await ext.storage.local.remove(ENGINE_STORAGE.stepResult);
  await cleanupTemporaryData();
  await stopKeepalive();
  await setEnginePhase(ENGINE_PHASE.idle);

  activeRun = { cancelled: false, chatTabId: null, labsTabId: null };
  await closeWorkflowTabs();

  if (isLoop) {
    const loopData = await ext.storage.local.get(LOOP_STORAGE.isRunning);
    if (!loopData[LOOP_STORAGE.isRunning]) return;

    await setRunState({
      [RUN_STORAGE.status]: RUN_STATUS.running,
      [RUN_STORAGE.error]: errorMsg,
      [RUN_STORAGE.step]: "Loop: falha após 2 tentativas. Aguardando próximo ciclo (2h)…",
    });

    await scheduleNextLoopCycle({ incrementCount: false, failureMessage: errorMsg });
    return;
  }

  await setRunState({
    [RUN_STORAGE.status]: RUN_STATUS.error,
    [RUN_STORAGE.error]: errorMsg,
    [RUN_STORAGE.step]: "Erro no fluxo.",
  });
}

async function checkStepTimeout() {
  const phase = await getEnginePhase();
  if (
    phase !== ENGINE_PHASE.json &&
    phase !== ENGINE_PHASE.image &&
    phase !== ENGINE_PHASE.export
  ) {
    return;
  }

  const data = await ext.storage.local.get([
    ENGINE_STORAGE.stepStartedAt,
    RUN_STORAGE.imageTotal,
  ]);
  const startedAt = Number(data[ENGINE_STORAGE.stepStartedAt]);
  if (!startedAt) return;

  let timeout = STEP_TIMEOUT_BY_PHASE[phase] || 300000;
  if (phase === ENGINE_PHASE.image) {
    const total = Number(data[RUN_STORAGE.imageTotal]) || 1;
    timeout = STEP_TIMEOUT_BY_PHASE.image * total;
  }

  if (Date.now() - startedAt <= timeout) return;

  await handleStepFailure(
    `Tempo esgotado aguardando etapa ${phaseLabel(phase)}.`,
    phase
  );
}

async function beginJsonStep() {
  await throwIfCancelled();
  await setEnginePhase(ENGINE_PHASE.json);
  await logAndSetWorkflowStep(ENGINE_PHASE.json, "Abrindo ChatGPT e pedindo JSON…");
  await ext.storage.local.set({
    [RUN_STORAGE.status]: RUN_STATUS.running,
    [RUN_STORAGE.cancelRequested]: false,
  });

  const data = await ext.storage.local.get([
    RUN_STORAGE.prompt,
    RUN_STORAGE.targetDurationSeconds,
  ]);
  const userPrompt = (data[RUN_STORAGE.prompt] || "").trim();
  if (!userPrompt) throw new Error("Prompt não configurado.");

  const duration = Number(data[RUN_STORAGE.targetDurationSeconds]) || 30;
  const jsonInstruction = buildJsonInstruction(duration);
  const recentJsons = await getRecentJsonResponses(ext);
  const prompt = buildJsonVariationPrompt(userPrompt, recentJsons, jsonInstruction);

  const tabId = await prepareChatGptTab();
  await fireTabMessage(
    tabId,
    { action: "RUN_TEXT", prompt, newChat: true, requireJson: true },
    { required: true }
  );
  await markStepAttemptStarted();
  await tickWorkflowTabs();
}

async function beginImageStep() {
  await throwIfCancelled();
  await setEnginePhase(ENGINE_PHASE.image);

  const data = await ext.storage.local.get([
    RUN_STORAGE.workflowScript,
    RUN_STORAGE.imageIndex,
    RUN_STORAGE.imageTotal,
    WORKFLOW_TAB_STORAGE.labsTabId,
  ]);

  const rawScript = (data[RUN_STORAGE.workflowScript] || "").trim();
  if (!rawScript) {
    await setEnginePhase(ENGINE_PHASE.json);
    scheduleEngineStep(500);
    return;
  }

  let script;
  try {
    script = JSON.parse(data[RUN_STORAGE.workflowScript] || "{}");
  } catch {
    throw new Error("Roteiro não encontrado no storage.");
  }

  const scenes = script.visual_scenes || [];
  const imageIndex = Number(data[RUN_STORAGE.imageIndex]) || 0;
  const imageTotal = Number(data[RUN_STORAGE.imageTotal]) || scenes.length;

  if (!scenes[imageIndex]) {
    throw new Error(`Cena ${imageIndex + 1} não encontrada no roteiro.`);
  }

  await logAndSetWorkflowStep(
    ENGINE_PHASE.image,
    `Gerando imagem ${imageIndex + 1}/${imageTotal} no Google Flow…`
  );

  const promptImagem = (scenes[imageIndex].prompt_en || "").trim();
  if (!promptImagem) throw new Error("prompt_en vazio na cena atual.");

  let tabId = data[WORKFLOW_TAB_STORAGE.labsTabId] || activeRun.labsTabId;
  try {
    if (tabId != null) await ext.tabs.get(tabId);
    else throw new Error("missing");
  } catch {
    tabId = await prepareLabsFlowTab();
  }

  activeRun.labsTabId = tabId;
  await persistWorkflowTabIds();

  await activateWorkflowTab(tabId);
  await ensureContentScript(tabId, LABS_CONTENT_SCRIPTS);

  await fireTabMessage(tabId, { action: "RUN_IMAGE", prompt: promptImagem }, { required: true });
  await markStepAttemptStarted();
  await tickWorkflowTabs();
}

async function beginExportStep() {
  await throwIfCancelled();
  await setEnginePhase(ENGINE_PHASE.export);
  await logAndSetWorkflowStep(ENGINE_PHASE.export, "Enviando pacote ao bridge…");
  await markStepAttemptStarted();

  const data = await ext.storage.local.get([
    RUN_STORAGE.workflowScript,
    RUN_STORAGE.imagesDataUrls,
    RUN_STORAGE.bridgeUrl,
    RUN_STORAGE.bridgeToken,
    RUN_STORAGE.channelId,
    RUN_STORAGE.jsonRaw,
  ]);

  let script;
  try {
    script = JSON.parse(data[RUN_STORAGE.workflowScript] || "{}");
  } catch {
    await handleStepFailure("Roteiro inválido para exportação.", ENGINE_PHASE.export);
    return;
  }

  const imagesDataUrls = Array.isArray(data[RUN_STORAGE.imagesDataUrls])
    ? data[RUN_STORAGE.imagesDataUrls]
    : [];

  try {
    const result = await exportContentPackage({
      bridgeUrl: data[RUN_STORAGE.bridgeUrl] || DEFAULT_BRIDGE_URL,
      bridgeToken: data[RUN_STORAGE.bridgeToken] || "",
      channelId: data[RUN_STORAGE.channelId] || DEFAULT_CHANNEL_ID,
      script,
      imagesDataUrls,
    });

    const isLoop = await isEngineLoop();
    await resetStepAttempt();
    await addRecentJsonResponse(ext, data[RUN_STORAGE.jsonRaw] || JSON.stringify(script));
    await finalizeExport(isLoop, result);
  } catch (err) {
    await handleStepFailure(err?.message || "Falha ao exportar pacote.", ENGINE_PHASE.export);
  }
}

async function handleStepComplete(payload) {
  const step = payload?.step;
  if (!step) return;

  await ext.storage.local.remove(ENGINE_STORAGE.stepResult);

  const engineData = await ext.storage.local.get(ENGINE_STORAGE.isLoop);
  const isLoop = !!engineData[ENGINE_STORAGE.isLoop];

  if (step === "RUN_TEXT") {
    if (!payload.ok) {
      await handleStepFailure(payload.error || "Falha no JSON.", ENGINE_PHASE.json);
      return;
    }
    let parsed;
    try {
      parsed = parseWorkflowJson(payload.response);
    } catch (err) {
      await handleStepFailure(err?.message || "JSON inválido.", ENGINE_PHASE.json);
      return;
    }
    await ext.storage.local.set({
      [RUN_STORAGE.roteiroPost]: parsed.script_text,
      [RUN_STORAGE.promptImagem]: parsed.prompt_imagem,
      [RUN_STORAGE.jsonRaw]: payload.response,
      [RUN_STORAGE.workflowScript]: JSON.stringify(parsed),
      [RUN_STORAGE.imageIndex]: 0,
      [RUN_STORAGE.imageTotal]: parsed.visual_scenes.length,
      [RUN_STORAGE.imagesDataUrls]: [],
    });
    await addRecentJsonResponse(ext, payload.response);
    await resetStepAttempt();
    await setEnginePhase(ENGINE_PHASE.image);
    scheduleEngineStep(2000);
    return;
  }

  if (step === "RUN_IMAGE") {
    if (!payload.ok) {
      await handleStepFailure(payload.error || "Falha na imagem.", ENGINE_PHASE.image);
      return;
    }

    const data = await ext.storage.local.get([
      RUN_STORAGE.imagesDataUrls,
      RUN_STORAGE.imageIndex,
      RUN_STORAGE.imageTotal,
    ]);
    const images = Array.isArray(data[RUN_STORAGE.imagesDataUrls])
      ? [...data[RUN_STORAGE.imagesDataUrls]]
      : [];
    images.push(payload.imageDataUrl);

    const imageIndex = Number(data[RUN_STORAGE.imageIndex]) || 0;
    const imageTotal = Number(data[RUN_STORAGE.imageTotal]) || images.length;

    await ext.storage.local.set({
      [RUN_STORAGE.imagesDataUrls]: images,
      [RUN_STORAGE.imageDataUrl]: payload.imageDataUrl,
      [RUN_STORAGE.imageIndex]: imageIndex + 1,
    });

    await resetStepAttempt();

    if (imageIndex + 1 < imageTotal) {
      await setEnginePhase(ENGINE_PHASE.image);
      scheduleEngineStep(2000);
      return;
    }

    await setEnginePhase(ENGINE_PHASE.export);
    scheduleEngineStep(2000);
    return;
  }
}

async function runEngineStep() {
  const phase = await getEnginePhase();

  try {
    if (phase === ENGINE_PHASE.json) {
      await beginJsonStep();
      return;
    }
    if (phase === ENGINE_PHASE.image) {
      await beginImageStep();
      return;
    }
    if (phase === ENGINE_PHASE.export) {
      await beginExportStep();
      return;
    }
  } catch (err) {
    const msg = err?.message || String(err);
    if (msg.includes("cancelad")) throw err;
    await handleStepFailure(msg, phase);
  }
}

async function processPendingStepResult() {
  const data = await ext.storage.local.get(ENGINE_STORAGE.stepResult);
  const result = data[ENGINE_STORAGE.stepResult];
  if (!result?.step) return false;
  await handleStepComplete(result);
  return true;
}

async function tickOneTab(tabId, phase) {
  await activateWorkflowTab(tabId);
  const files =
    phase === ENGINE_PHASE.image
      ? LABS_CONTENT_SCRIPTS
      : ["shared/constants.js", "shared/parse-json.js", "content/chatgpt.js"];
  try {
    await ext.tabs.sendMessage(tabId, { action: "WORKFLOW_TICK" });
    return;
  } catch {
    /* injeta */
  }
  try {
    await ext.scripting.executeScript({ target: { tabId }, files });
    await ext.tabs.sendMessage(tabId, { action: "WORKFLOW_TICK" });
  } catch {
    /* próximo keepalive */
  }
}

async function tickWorkflowTabs() {
  await loadWorkflowTabIds();
  const phase = await getEnginePhase();

  if (phase === ENGINE_PHASE.json) {
    if (activeRun.chatTabId != null) await tickOneTab(activeRun.chatTabId, phase);
    return;
  }
  if (phase === ENGINE_PHASE.image) {
    if (activeRun.labsTabId != null) await tickOneTab(activeRun.labsTabId, phase);
  }
}

async function onKeepalive() {
  const phase = await getEnginePhase();
  if (phase === ENGINE_PHASE.idle) {
    await stopKeepalive();
    return;
  }

  if (
    phase === ENGINE_PHASE.json ||
    phase === ENGINE_PHASE.image ||
    phase === ENGINE_PHASE.export
  ) {
    if (phase !== ENGINE_PHASE.export) {
      await tickWorkflowTabs();
    }
    await checkStepTimeout();
  }

  await processPendingStepResult();
  await rescheduleKeepalive();
}

async function startEngine({ isLoop }) {
  activeRun = { cancelled: false, chatTabId: null, labsTabId: null };

  await logStageStart(ext, "cycle_start", isLoop ? "modo loop" : "execução única");

  await resetStepAttempt();
  await ext.storage.local.remove(ENGINE_STORAGE.stepResult);
  await ext.storage.local.remove([
    RUN_STORAGE.workflowScript,
    RUN_STORAGE.jsonRaw,
    RUN_STORAGE.roteiroPost,
    RUN_STORAGE.imagesDataUrls,
    RUN_STORAGE.imageDataUrl,
    RUN_STORAGE.imageIndex,
    RUN_STORAGE.imageTotal,
  ]);

  await ext.storage.local.set({
    [ENGINE_STORAGE.isLoop]: isLoop,
    [ENGINE_STORAGE.phase]: ENGINE_PHASE.json,
    [RUN_STORAGE.cancelRequested]: false,
    [RUN_STORAGE.status]: RUN_STATUS.running,
    [RUN_STORAGE.error]: "",
  });

  await startKeepalive();
  scheduleEngineStep(800);
}

async function stopEngine({ closeTabs = true } = {}) {
  await stopKeepalive();
  await ext.alarms.clear(ENGINE_ALARMS.step);
  await ext.alarms.clear(LOOP_CONFIG.closeChatgptAlarmName);
  await setEnginePhase(ENGINE_PHASE.idle);
  activeRun.cancelled = true;
  if (closeTabs) await closeWorkflowTabs();
}

async function cancelRun() {
  activeRun.cancelled = true;
  await ext.storage.local.set({
    [RUN_STORAGE.cancelRequested]: true,
    [RUN_STORAGE.status]: RUN_STATUS.cancelled,
  });
  await stopEngine({ closeTabs: true });
  await logStageStart(ext, "cancelled");
  await setStep("Cancelado.");
}

async function finishCloseTabsAndScheduleNext() {
  const loopData = await ext.storage.local.get([
    LOOP_STORAGE.isRunning,
    ENGINE_STORAGE.isLoop,
  ]);

  const isLoop = !!loopData[LOOP_STORAGE.isRunning] || !!loopData[ENGINE_STORAGE.isLoop];

  if (!isLoop) {
    await setEnginePhase(ENGINE_PHASE.idle);
    await stopKeepalive();
    await logStageStart(ext, "cycle_done", "execução única concluída");
    await setRunState({
      [RUN_STORAGE.status]: RUN_STATUS.done,
      [RUN_STORAGE.step]: "Pacote exportado. Abas fechadas.",
    });
    return;
  }

  await scheduleNextLoopCycle({ incrementCount: true });
}

async function startLoop() {
  loopState.isRunning = true;
  await ext.storage.local.set({
    [LOOP_STORAGE.isRunning]: true,
    [LOOP_STORAGE.iterationCount]: 0,
  });
  await startEngine({ isLoop: true });
}

async function stopLoop() {
  loopState.isRunning = false;
  await ext.alarms.clear(LOOP_CONFIG.alarmName);
  await ext.storage.local.set({
    [LOOP_STORAGE.isRunning]: false,
    [LOOP_STORAGE.nextRunAt]: null,
  });
  await stopEngine();
  await logStageStart(ext, "loop_stopped");
  await setRunState({
    [RUN_STORAGE.status]: RUN_STATUS.cancelled,
    [RUN_STORAGE.step]: "Loop parado.",
  });
}

async function runLoopIteration() {
  const data = await ext.storage.local.get([
    RUN_STORAGE.prompt,
    RUN_STORAGE.bridgeUrl,
    RUN_STORAGE.channelId,
    LOOP_STORAGE.isRunning,
  ]);

  if (!data[LOOP_STORAGE.isRunning]) return;

  const configError = validateContentConfig(
    (data[RUN_STORAGE.prompt] || "").trim(),
    (data[RUN_STORAGE.bridgeUrl] || DEFAULT_BRIDGE_URL).trim(),
    (data[RUN_STORAGE.channelId] || DEFAULT_CHANNEL_ID).trim()
  );
  if (configError) {
    await stopLoop();
    return;
  }

  try {
    await startEngine({ isLoop: true });
  } catch (err) {
    const msg = err?.message || String(err);
    if (msg.includes("cancelad")) {
      await stopLoop();
      return;
    }
    await recoverAfterStepFailure(msg);
  }
}

function persistWorkflowConfig(message) {
  return ext.storage.local.set({
    [RUN_STORAGE.prompt]: (message.prompt || "").trim(),
    [RUN_STORAGE.bridgeUrl]: (message.bridgeUrl || DEFAULT_BRIDGE_URL).trim(),
    [RUN_STORAGE.channelId]: (message.channelId || DEFAULT_CHANNEL_ID).trim(),
    [RUN_STORAGE.bridgeToken]: (message.bridgeToken || "").trim(),
    [RUN_STORAGE.targetDurationSeconds]: Number(message.targetDurationSeconds) || 30,
    [RUN_STORAGE.flowProjectUrl]: (message.flowProjectUrl || "").trim(),
  });
}

ext.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ENGINE_ALARMS.keepalive) {
    onKeepalive();
    return;
  }
  if (alarm.name === ENGINE_ALARMS.step) {
    runEngineStep().catch(async (err) => {
      const msg = err?.message || String(err);
      if (msg.includes("cancelad")) return;
      const phase = await getEnginePhase();
      await handleStepFailure(msg, phase);
    });
    return;
  }
  if (alarm.name === LOOP_CONFIG.closeChatgptAlarmName) {
    onCloseChatgptAlarm().catch(async (err) => {
      const msg = err?.message || String(err);
      await appendWorkflowLog(ext, { kind: "error", message: msg, stage: "error" });
      await setStep(`Erro ao fechar abas: ${msg}`);
    });
    return;
  }
  if (alarm.name === LOOP_CONFIG.alarmName) {
    runLoopIteration();
  }
});

ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.action === "FLOW_PROJECT_URL") {
    const url = (message.url || "").trim();
    if (url && isLabsFlowUrl(url)) {
      ext.storage.local.set({ [RUN_STORAGE.flowProjectUrl]: url.split("#")[0] });
    }
    sendResponse({ ok: true });
    return false;
  }

  if (message?.action === "FLOW_CLICK_SUBMIT_FALLBACK") {
    const tabId = _sender.tab?.id ?? activeRun.labsTabId;
    clickFlowSubmitInPage(tabId)
      .then((result) => sendResponse(result || { ok: false, reason: "no-result" }))
      .catch((err) =>
        sendResponse({ ok: false, reason: err?.message || String(err) })
      );
    return true;
  }

  if (message?.action === "FLOW_PAGE_BRIDGE") {
    const tabId = _sender.tab?.id ?? activeRun.labsTabId;
    runFlowPageBridge(tabId, message.bridgeAction || "clickSubmit", {
      prompt: message.prompt || "",
    })
      .then((result) => sendResponse(result || { ok: false, reason: "no-result" }))
      .catch((err) =>
        sendResponse({ ok: false, reason: err?.message || String(err) })
      );
    return true;
  }

  if (message?.action === "WORKFLOW_STEP_DONE") {
    handleStepComplete(message)
      .then(() => sendResponse({ ok: true }))
      .catch(async (err) => {
        const msg = err?.message || String(err);
        if (msg.includes("cancelad")) {
          sendResponse({ ok: false, error: msg });
          return;
        }
        const phase = stepNameToPhase(message?.step) || (await getEnginePhase());
        await handleStepFailure(msg, phase);
        sendResponse({ ok: false, error: msg });
      });
    return true;
  }

  if (message?.action === "START_WORKFLOW") {
    const configError = validateContentConfig(
      (message.prompt || "").trim(),
      (message.bridgeUrl || DEFAULT_BRIDGE_URL).trim(),
      (message.channelId || DEFAULT_CHANNEL_ID).trim()
    );
    if (configError) {
      sendResponse({ ok: false, error: configError });
      return false;
    }
    persistWorkflowConfig(message).then(() => {
      if (message.tabId != null) activeRun.chatTabId = message.tabId;
      startEngine({ isLoop: false });
      sendResponse({ ok: true, started: true });
    });
    return true;
  }

  if (message?.action === "START_LOOP") {
    const configError = validateContentConfig(
      (message.prompt || "").trim(),
      (message.bridgeUrl || DEFAULT_BRIDGE_URL).trim(),
      (message.channelId || DEFAULT_CHANNEL_ID).trim()
    );
    if (configError) {
      sendResponse({ ok: false, error: configError });
      return false;
    }
    persistWorkflowConfig(message).then(() => {
      startLoop();
      sendResponse({ ok: true, started: true });
    });
    return true;
  }

  if (message?.action === "STOP_LOOP") {
    stopLoop().then(() => sendResponse({ ok: true }));
    return true;
  }

  if (message?.action === "CANCEL_RUN") {
    cancelRun()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err?.message }));
    return true;
  }

  if (message?.action === "POPUP_OPENED") {
    clearUnreadBadge(ext).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (message?.action === "SAVE_FORM_BACKUP") {
    saveFormBackupToChatGpt(message.data || {})
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err?.message }));
    return true;
  }

  if (message?.action === "GET_FORM_BACKUP") {
    loadFormBackupFromChatGpt()
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err?.message }));
    return true;
  }

  if (message?.action === "GET_LOOP_STATUS") {
    ext.storage.local
      .get([
        LOOP_STORAGE.isRunning,
        LOOP_STORAGE.iterationCount,
        LOOP_STORAGE.lastRunAt,
        LOOP_STORAGE.nextRunAt,
      ])
      .then((data) => {
        sendResponse({
          ok: true,
          isRunning: !!data[LOOP_STORAGE.isRunning],
          iterationCount: data[LOOP_STORAGE.iterationCount] || 0,
          lastRunAt: data[LOOP_STORAGE.lastRunAt],
          nextRunAt: data[LOOP_STORAGE.nextRunAt],
        });
      });
    return true;
  }

  return false;
});

ext.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes[ENGINE_STORAGE.stepResult]) return;
  processPendingStepResult();
});

ext.storage.local.get([LOOP_STORAGE.isRunning, ENGINE_STORAGE.phase]).then((data) => {
  if (data[LOOP_STORAGE.isRunning]) {
    loopState.isRunning = true;
  }
  const phase = data[ENGINE_STORAGE.phase];
  if (phase && phase !== ENGINE_PHASE.idle) {
    startKeepalive();
  }
});
