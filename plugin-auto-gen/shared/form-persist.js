/* global FORM_BACKUP_KEY, RUN_STORAGE */

globalThis.buildFormBackupPayload = function buildFormBackupPayload({
  prompt,
  bridgeUrl,
  channelId,
  bridgeToken,
  targetDurationSeconds,
  flowProjectUrl,
}) {
  return {
    prompt: prompt || "",
    bridgeUrl: bridgeUrl || "",
    channelId: channelId || "",
    bridgeToken: bridgeToken || "",
    targetDurationSeconds: Number(targetDurationSeconds) || 30,
    flowProjectUrl: flowProjectUrl || "",
  };
};

globalThis.readPopupFormBackup = function readPopupFormBackup() {
  try {
    const raw = localStorage.getItem(FORM_BACKUP_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

globalThis.writePopupFormBackup = function writePopupFormBackup(data) {
  try {
    localStorage.setItem(FORM_BACKUP_KEY, JSON.stringify(data));
  } catch {
    // Quota ou modo privado — ignora.
  }
};

globalThis.applyFormBackup = function applyFormBackup(backup) {
  if (!backup) return {};
  return {
    [RUN_STORAGE.prompt]: backup.prompt || "",
    [RUN_STORAGE.bridgeUrl]: backup.bridgeUrl || "",
    [RUN_STORAGE.channelId]: backup.channelId || "",
    [RUN_STORAGE.bridgeToken]: backup.bridgeToken || "",
    [RUN_STORAGE.targetDurationSeconds]: Number(backup.targetDurationSeconds) || 30,
    [RUN_STORAGE.flowProjectUrl]: backup.flowProjectUrl || "",
  };
};

globalThis.isFormBackupEmpty = function isFormBackupEmpty(backup) {
  if (!backup) return true;
  return !backup.prompt && !backup.bridgeUrl && !backup.channelId;
};

globalThis.readPageFormBackup = function readPageFormBackup() {
  try {
    const raw = localStorage.getItem(FORM_BACKUP_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

globalThis.writePageFormBackup = function writePageFormBackup(data) {
  try {
    localStorage.setItem(FORM_BACKUP_KEY, JSON.stringify(data));
  } catch {
    // Ignora falhas de quota no site.
  }
};
