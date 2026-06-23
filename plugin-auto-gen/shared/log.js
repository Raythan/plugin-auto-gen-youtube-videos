/* global LOG_STORAGE, LOG_MAX_ENTRIES, LOG_MAX_JSONS, formatStepLogLabel */

globalThis.LOG_STORAGE = {
  entries: "workflowLogEntries",
  recentPosts: "workflowRecentPosts",
  recentJsons: "workflowRecentJsons",
  unreadCount: "workflowUnreadPostCount",
};

globalThis.LOG_MAX_ENTRIES = 100;
globalThis.LOG_MAX_JSONS = 10;

globalThis.LOG_STAGE = {
  cycle_start: "Ciclo — início",
  json: "JSON (ChatGPT)",
  image: "Imagens (Google Flow)",
  export: "Exportar pacote",
  close_chatgpt: "Fechamento — aba ChatGPT",
  cycle_done: "Ciclo concluído",
  cancelled: "Execução cancelada",
  loop_stopped: "Loop interrompido",
  error: "Erro",
  info: "Info",
};

globalThis.appendWorkflowLog = async function appendWorkflowLog(
  ext,
  { kind = "info", message, stage = null }
) {
  const at = new Date().toISOString();
  const data = await ext.storage.local.get(LOG_STORAGE.entries);
  const entries = Array.isArray(data[LOG_STORAGE.entries]) ? data[LOG_STORAGE.entries] : [];
  entries.unshift({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    at,
    kind,
    message: message || "",
    stage,
  });
  if (entries.length > LOG_MAX_ENTRIES) entries.length = LOG_MAX_ENTRIES;
  await ext.storage.local.set({ [LOG_STORAGE.entries]: entries });
};

globalThis.logStageStart = async function logStageStart(ext, stage, extra = "") {
  const label = LOG_STAGE[stage] || stage;
  const message = extra ? `${label} — ${extra}` : label;
  await appendWorkflowLog(ext, { kind: "stage", message, stage });
};

globalThis.logWorkflowStage = async function logWorkflowStage(ext, phaseId, extra = "") {
  const label = formatStepLogLabel(phaseId);
  const message = extra ? `${label} — ${extra}` : label;
  await appendWorkflowLog(ext, { kind: "stage", message, stage: phaseId });
};

globalThis.addRecentJsonResponse = async function addRecentJsonResponse(ext, jsonRaw) {
  const raw = (jsonRaw || "").trim();
  if (!raw) return;
  const data = await ext.storage.local.get(LOG_STORAGE.recentJsons);
  const jsons = Array.isArray(data[LOG_STORAGE.recentJsons]) ? data[LOG_STORAGE.recentJsons] : [];
  jsons.unshift({
    id: `json-${Date.now()}`,
    generatedAt: new Date().toISOString(),
    jsonRaw: raw,
  });
  if (jsons.length > LOG_MAX_JSONS) jsons.length = LOG_MAX_JSONS;
  await ext.storage.local.set({ [LOG_STORAGE.recentJsons]: jsons });
};

globalThis.getRecentJsonResponses = async function getRecentJsonResponses(ext) {
  const data = await ext.storage.local.get(LOG_STORAGE.recentJsons);
  const jsons = Array.isArray(data[LOG_STORAGE.recentJsons]) ? data[LOG_STORAGE.recentJsons] : [];
  return jsons.map((entry) => entry.jsonRaw).filter(Boolean);
};

globalThis.buildJsonVariationPrompt = function buildJsonVariationPrompt(userPrompt, recentJsons, jsonInstruction) {
  const base = (userPrompt || "").trim();
  const instruction = jsonInstruction || "";
  if (!recentJsons?.length) {
    return instruction ? `${base}\n\n${instruction}` : base;
  }

  const historyBlock = recentJsons
    .map((json, index) => `--- Resposta ${index + 1} ---\n${json.trim()}`)
    .join("\n\n");

  const instructionBlock = instruction ? `\n\n${instruction}` : "";

  return `${historyBlock}

---

Com base nos JSONs acima, gere um NOVO conteúdo com ampla variação em relação a todos eles (tema, abordagem, tom, estrutura e ideia central devem ser diferentes). Não repita nem parafraseie posts anteriores.

Solicitação do usuário:
${base}${instructionBlock}`;
};

globalThis.syncActionBadge = async function syncActionBadge(ext) {
  const data = await ext.storage.local.get(LOG_STORAGE.unreadCount);
  const count = Number(data[LOG_STORAGE.unreadCount]) || 0;
  const text = count > 0 ? (count > 99 ? "99+" : String(count)) : "";

  const apply = async (api) => {
    if (!api) return;
    await api.setBadgeText({ text });
    if (count > 0) {
      await api.setBadgeBackgroundColor({ color: "#E02020" });
      if (api.setBadgeTextColor) {
        await api.setBadgeTextColor({ color: "#FFFFFF" });
      }
    }
  };

  try {
    await apply(ext.action);
  } catch {
  }
  if (ext.browserAction) {
    try {
      await apply(ext.browserAction);
    } catch {
      /* ignore */
    }
  }
};

globalThis.clearUnreadBadge = async function clearUnreadBadge(ext) {
  await ext.storage.local.set({ [LOG_STORAGE.unreadCount]: 0 });
  await syncActionBadge(ext);
};
