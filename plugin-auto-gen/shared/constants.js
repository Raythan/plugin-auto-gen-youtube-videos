globalThis.CHATGPT_URL = "https://chatgpt.com/";
globalThis.LABS_FLOW_ENTRY_URL = "https://labs.google/fx/tools/image-fx";
/** @deprecated use LABS_FLOW_ENTRY_URL */
globalThis.LABS_IMAGEFX_URL = globalThis.LABS_FLOW_ENTRY_URL;
globalThis.FORM_BACKUP_KEY = "plugin-auto-gen-form";
globalThis.DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765/content";
globalThis.DEFAULT_CHANNEL_ID = "rpjtechgroup";

globalThis.RUN_STORAGE = {
  prompt: "savedPrompt",
  response: "lastResponse",
  promptSent: "lastPromptSent",
  status: "runStatus",
  error: "lastError",
  updatedAt: "lastUpdatedAt",
  step: "runStep",
  activeTabId: "activeTabId",
  cancelRequested: "cancelRequested",
  roteiroPost: "roteiroPost",
  promptImagem: "promptImagem",
  jsonRaw: "jsonRaw",
  imageDataUrl: "imageDataUrl",
  imagesDataUrls: "workflowImagesDataUrls",
  imageIndex: "workflowImageIndex",
  imageTotal: "workflowImageTotal",
  workflowScript: "workflowScriptJson",
  workflowPhase: "workflowPhase",
  bridgeUrl: "bridgeUrl",
  channelId: "channelId",
  bridgeToken: "bridgeToken",
  targetDurationSeconds: "targetDurationSeconds",
  flowProjectUrl: "flowProjectUrl",
};

globalThis.RUN_STATUS = {
  idle: "idle",
  running: "running",
  done: "done",
  error: "error",
  cancelled: "cancelled",
};

globalThis.WORKFLOW_PHASE = {
  idle: "idle",
  json: "json",
  image: "image",
  export: "export",
  complete: "complete",
};

globalThis.LOOP_STORAGE = {
  isRunning: "loopIsRunning",
  iterationCount: "loopIterationCount",
  lastRunAt: "loopLastRunAt",
  nextRunAt: "loopNextRunAt",
};

globalThis.LOOP_CONFIG = {
  alarmName: "hourly-post-loop",
  closeChatgptAlarmName: "workflow-close-chatgpt",
  intervalMinutes: 120,
  closeChatgptDelaySeconds: 10,
};

globalThis.WORKFLOW_TAB_STORAGE = {
  chatTabId: "workflowChatTabId",
  labsTabId: "workflowLabsTabId",
};

globalThis.ENGINE_STORAGE = {
  phase: "enginePhase",
  isLoop: "engineIsLoop",
  stepResult: "workflowStepResult",
  stepAttempt: "engineStepAttempt",
  stepStartedAt: "engineStepStartedAt",
};

globalThis.STEP_MAX_ATTEMPTS = 2;

globalThis.STEP_TIMEOUT_BY_PHASE = {
  json: 360000,
  image: 660000,
  export: 120000,
};

globalThis.STEP_RETRY_DELAY_MS = 3000;
globalThis.LOOP_CYCLE_RESTART_DELAY_MS = 5000;

globalThis.ENGINE_PHASE = {
  idle: "idle",
  json: "json",
  image: "image",
  export: "export",
  closingChatgpt: "closing-chatgpt",
};

globalThis.ENGINE_ALARMS = {
  step: "workflow-step",
  keepalive: "workflow-keepalive",
};

globalThis.buildWorkflowSteps = function buildWorkflowSteps() {
  return [
    { id: ENGINE_PHASE.json, short: "JSON", label: "JSON (ChatGPT)" },
    { id: ENGINE_PHASE.image, short: "imagens", label: "Imagens (Google Flow)" },
    { id: ENGINE_PHASE.export, short: "export", label: "Exportar pacote" },
  ];
};

globalThis.formatStepLogLabel = function formatStepLogLabel(phaseId) {
  const steps = buildWorkflowSteps();
  const idx = steps.findIndex((s) => s.id === phaseId);
  if (idx < 0) return `Etapa — ${phaseId}`;
  return `Etapa ${idx + 1}/${steps.length} — ${steps[idx].label}`;
};

globalThis.formatStepProgress = function formatStepProgress(phaseId) {
  const steps = buildWorkflowSteps();
  const idx = steps.findIndex((s) => s.id === phaseId);
  if (idx < 0) return "";
  return `${idx + 1}/${steps.length}`;
};

globalThis.formatPhaseHint = function formatPhaseHint(phaseId) {
  const steps = buildWorkflowSteps();
  const idx = steps.findIndex((s) => s.id === phaseId);
  if (idx < 0) return "";
  const step = steps[idx];
  return `etapa ${idx + 1}/${steps.length} — ${step.short}`;
};

globalThis.isLabsFlowUrl = function isLabsFlowUrl(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (host !== "labs.google" && !host.endsWith(".labs.google")) return false;
    return /\/tools\/flow\/project\//i.test(parsed.pathname);
  } catch {
    return /labs\.google.*\/tools\/flow\/project\//i.test(url);
  }
};

globalThis.extractFlowProjectId = function extractFlowProjectId(url) {
  if (!url) return null;
  const match = String(url).match(
    /\/tools\/flow\/project\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i
  );
  return match ? match[1] : null;
};

globalThis.isLabsFlowTabUrl = function isLabsFlowTabUrl(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (host !== "labs.google" && !host.endsWith(".labs.google")) return false;
    if (globalThis.isLabsFlowUrl(url)) return true;
    return /image-fx|imagefx/i.test(parsed.pathname);
  } catch {
    return /labs\.google/i.test(url);
  }
};

globalThis.buildJsonInstruction = function buildJsonInstruction(targetDurationSeconds) {
  const sceneCount = globalThis.computeSceneCount(targetDurationSeconds);
  return (
    `Responda APENAS com um JSON válido (sem markdown) contendo: ` +
    `"title", "script_text" (roteiro falado PT-BR, 55-95 palavras), ` +
    `"youtube_body", "tags" (mínimo 3), ` +
    `"visual_scenes" (exatamente ${sceneCount} objetos com "prompt_en" em inglês e "keywords_pt"), ` +
    `"topic_key" (kebab-case).`
  );
};
