/* global RUN_STORAGE, RUN_STATUS, ENGINE_STORAGE, IMAGE_TIMEOUT_MS, isLabsFlowUrl, isFlowGeneratedImageSrc, captureImageAsDataUrl, collectFlowGeneratedImages, pickBestImage, findFlowComposerRoot, findFlowSubmitButton, findFlowAddMenuButton, isFlowAddMenuButton, isFlowSubmitButtonEnabled, isFlowAddMenuOpen, findFlowPromptInput */

const ext = typeof browser !== "undefined" ? browser : chrome;

if (!globalThis.__labsFlowBootstrapped) {
  globalThis.__labsFlowBootstrapped = true;

  let abortRequested = false;
  /** @type {null | { startedAt: number, lastSrc: string, imageStableMs: number, bestDataUrl: string, baselineSrcs: Set<string>, baselineTileIds: Set<string>, submitAttempts: number, lastSubmitAt: number, step: string }} */
  let workflowJob = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function notifyWorkflowStepDone(step, data) {
    const result = { step, at: Date.now(), ...data };
    await ext.storage.local.set({ [ENGINE_STORAGE.stepResult]: result });
    try {
      await ext.runtime.sendMessage({ action: "WORKFLOW_STEP_DONE", ...result });
    } catch {
      /* background lê storage no keepalive */
    }
  }

  async function persistFlowProjectUrl() {
    if (!isLabsFlowUrl(window.location.href)) return;
    const url = window.location.href.split("#")[0];
    await ext.storage.local.set({ [RUN_STORAGE.flowProjectUrl]: url });
    try {
      await ext.runtime.sendMessage({ action: "FLOW_PROJECT_URL", url });
    } catch {
      /* ignore */
    }
  }

  async function shouldAbort() {
    if (abortRequested) return true;
    const data = await ext.storage.local.get([
      RUN_STORAGE.cancelRequested,
      RUN_STORAGE.status,
    ]);
    return (
      !!data[RUN_STORAGE.cancelRequested] ||
      data[RUN_STORAGE.status] === RUN_STATUS.cancelled
    );
  }

  async function throwIfAborted() {
    if (await shouldAbort()) {
      throw new Error("Execução cancelada pelo usuário.");
    }
  }

  function queryFirst(selectors, root = document) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      if (el) return el;
    }
    return null;
  }

  function queryButtonByText(texts, root = document) {
    const normalized = texts.map((t) => t.toLowerCase());
    const buttons = root.querySelectorAll("button, a, [role='button']");
    for (const btn of buttons) {
      const label = (btn.getAttribute("aria-label") || btn.textContent || "")
        .trim()
        .toLowerCase();
      if (normalized.some((t) => label.includes(t))) return btn;
    }
    return null;
  }

  function findComposerRoot() {
    return findFlowComposerRoot();
  }

  function findSubmitButton() {
    return findFlowSubmitButton();
  }

  function findAddMenuButton() {
    return findFlowAddMenuButton(findComposerRoot());
  }

  function dispatchEscape() {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape", bubbles: true }));
  }

  async function closeComposerDialogs() {
    for (let i = 0; i < 3; i += 1) {
      const addBtn = findAddMenuButton();
      if (addBtn?.getAttribute("aria-expanded") === "true") {
        dispatchEscape();
        await sleep(250);
        continue;
      }
      const openMenu = document.querySelector(
        "button[aria-haspopup='menu'][aria-expanded='true'], button[aria-haspopup='dialog'][aria-expanded='true']"
      );
      if (openMenu && findComposerRoot()?.contains(openMenu)) {
        dispatchEscape();
        await sleep(250);
        continue;
      }
      break;
    }
  }

  function getPromptText() {
    const slate = findPromptInputSync();
    if (!slate) return "";
    return (slate.textContent || "").trim();
  }

  function snapshotTileIds() {
    const ids = new Set();
    document.querySelectorAll("[data-tile-id]").forEach((tile) => {
      const id = tile.getAttribute("data-tile-id");
      if (id) ids.add(id);
    });
    return ids;
  }

  function countFlowMediaImages() {
    let n = 0;
    document.querySelectorAll("img").forEach((img) => {
      if (img.closest("#flow-desktop-header")) return;
      const src = img.currentSrc || img.src || "";
      if (isFlowGeneratedImageSrc(src)) n += 1;
    });
    return n;
  }

  function pageText() {
    return (document.body?.innerText || "").toLowerCase();
  }

  function isBlockedOrLoginPage() {
    const text = pageText();
    if (/isn't available in your country|não está disponível no seu país/i.test(text)) {
      return "Google Flow não está disponível no seu país.";
    }
    if (/sign in to start|sign in with google|entrar com o google|faça login/i.test(text)) {
      return "Faça login no Google Flow e tente novamente.";
    }
    if (!isLabsFlowUrl(window.location.href) && !findPromptInputSync()) {
      if (queryButtonByText(["sign in", "entrar", "get notified"])) {
        return "Faça login no Google Flow e tente novamente.";
      }
    }
    return null;
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }

  function findPromptInputSync() {
    const slate = document.querySelector(
      '[role="textbox"][data-slate-editor="true"][contenteditable="true"]'
    );
    if (slate && isVisible(slate)) return slate;

    const candidates = [
      ...document.querySelectorAll("textarea"),
      ...document.querySelectorAll("[contenteditable='true']"),
      ...document.querySelectorAll("input[type='text']"),
    ];
    for (const el of candidates) {
      if (!isVisible(el)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 80) continue;
      return el;
    }
    return null;
  }

  async function waitForFlowProject(timeoutMs = 120000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await throwIfAborted();
      const blocked = isBlockedOrLoginPage();
      if (blocked) throw new Error(blocked);
      if (isLabsFlowUrl(window.location.href)) {
        await persistFlowProjectUrl();
        return;
      }
      await sleep(500);
    }
    throw new Error(
      "Redirecionamento para projeto Flow não concluído. Configure a URL do projeto no popup."
    );
  }

  async function waitForPromptInput(timeoutMs = 90000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await throwIfAborted();
      const blocked = isBlockedOrLoginPage();
      if (blocked) throw new Error(blocked);
      const el = findPromptInputSync();
      if (el) return el;
      await sleep(400);
    }
    throw new Error("Campo de prompt do Google Flow não encontrado.");
  }

  async function requestFlowPageBridge(bridgeAction, payload = {}) {
    try {
      return await ext.runtime.sendMessage({
        action: "FLOW_PAGE_BRIDGE",
        bridgeAction,
        ...payload,
      });
    } catch (err) {
      return { ok: false, reason: err?.message || String(err), bridgeFailed: true };
    }
  }

  async function submitPrompt(baselineTileIds, baselineSrcs) {
    const beforeTiles = baselineTileIds?.size ?? snapshotTileIds().size;
    const beforeImages = baselineSrcs?.size ?? countFlowMediaImages();
    const beforePromptLen = getPromptText().length;

    await closeComposerDialogs();

    for (let attempt = 0; attempt < 4; attempt += 1) {
      await closeComposerDialogs();

      const result = await requestFlowPageBridge("clickSubmit");
      if (result?.submitted || hasSubmitTakenEffect(beforeTiles, beforeImages, beforePromptLen)) {
        return true;
      }

      if (result?.wrongButton) {
        await closeComposerDialogs();
        await sleep(300);
        continue;
      }

      const btn = findSubmitButton();
      if (btn && isSubmitButtonEnabled(btn) && !isFlowAddMenuButton(btn)) {
        btn.scrollIntoView({ block: "center", inline: "nearest" });
        await sleep(120);
        btn.click();
        await sleep(400);
        if (hasSubmitTakenEffect(beforeTiles, beforeImages, beforePromptLen)) return true;
      }

      await sleep(300);
    }

    return false;
  }

  function dispatchPasteOnElement(el, text) {
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    el.dispatchEvent(
      new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt })
    );
    for (const type of ["beforeinput", "input"]) {
      el.dispatchEvent(
        new InputEvent(type, {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: text,
        })
      );
    }
  }

  function fillSlatePrompt(element, value) {
    const text = String(value || "");
    element.focus();
    element.click();
    try {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    } catch {
      element.textContent = "";
    }
    dispatchPasteOnElement(element, text);
    if ((element.textContent || "").trim().length === 0) {
      try {
        document.execCommand("insertText", false, text);
      } catch {
        element.textContent = text;
      }
      element.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertText", data: text })
      );
    }
  }

  function fillPrompt(element, value) {
    if (element.getAttribute("data-slate-editor") === "true") {
      fillSlatePrompt(element, value);
      return;
    }
    element.focus();
    const text = String(value || "");
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      element.value = text;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    if (element.isContentEditable) {
      element.textContent = text;
      element.dispatchEvent(new InputEvent("input", { bubbles: true, data: text }));
    }
  }

  async function trySetImageMode() {
    const addBtn = findAddMenuButton();
    if (addBtn && addBtn.getAttribute("aria-expanded") !== "true") {
      addBtn.click();
      await sleep(500);
    }
    const createImage = queryButtonByText([
      "criar imagem",
      "create image",
      "generate image",
    ]);
    if (createImage && !createImage.getAttribute("aria-haspopup")) {
      createImage.click();
      await sleep(500);
      dispatchEscape();
      await sleep(200);
      return true;
    }
    dispatchEscape();
    await sleep(200);
    return false;
  }

  async function trySetAspectRatio916() {
    const root = findComposerRoot() || document;
    const icons = root.querySelectorAll("i.google-symbols, i.material-symbols");
    for (const icon of icons) {
      if ((icon.textContent || "").trim() !== "crop_9_16") continue;
      const btn = icon.closest("button");
      if (!btn || btn.getAttribute("aria-haspopup") === "menu") continue;
      if (!btn.disabled) {
        btn.click();
        await sleep(400);
        return true;
      }
    }
    return false;
  }

  function isSubmitButtonEnabled(btn) {
    return isFlowSubmitButtonEnabled(btn);
  }

  function hasSubmitTakenEffect(beforeTiles, beforeImages, beforePromptLen) {
    const btn = findSubmitButton();
    const promptLen = getPromptText().length;
    const tilesGrew = snapshotTileIds().size > beforeTiles;
    const imagesGrew = countFlowMediaImages() > beforeImages;
    const promptCleared = promptLen === 0 && beforePromptLen > 0;
    const btnDisabledAfterPrompt =
      beforePromptLen > 0 && btn?.getAttribute("aria-disabled") === "true";
    return tilesGrew || imagesGrew || promptCleared || btnDisabledAfterPrompt;
  }

  async function waitForSubmitButtonEnabled(timeoutMs = 25000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const btn = findSubmitButton();
      if (isSubmitButtonEnabled(btn)) return btn;
      await sleep(200);
    }
    const btn = findSubmitButton();
    return isSubmitButtonEnabled(btn) ? btn : null;
  }

  function snapshotImageSrcs() {
    const srcs = new Set();
    collectFlowGeneratedImages().forEach((img) => {
      const src = img.currentSrc || img.src || "";
      if (src) srcs.add(src);
    });
    document.querySelectorAll("img").forEach((img) => {
      const src = img.currentSrc || img.src || "";
      if (src && isFlowGeneratedImageSrc(src)) srcs.add(src);
    });
    return srcs;
  }

  function isGenerating() {
    const submitBtn = findSubmitButton();
    const promptLen = getPromptText().length;

    if (promptLen === 0 && submitBtn?.getAttribute("aria-disabled") === "true") {
      return true;
    }

    const composer = findComposerRoot();
    if (
      composer &&
      queryFirst(["[aria-busy='true']", "[data-state='loading']"], composer)
    ) {
      return true;
    }

    for (const tile of document.querySelectorAll("[data-tile-id]")) {
      if (
        tile.querySelector(
          "[aria-busy='true'], [data-state='loading'], [class*='skeleton' i], [class*='shimmer' i]"
        )
      ) {
        return true;
      }
    }

    return false;
  }

  function findImageInTile(tile) {
    if (!tile || tile.closest("#flow-desktop-header")) return null;
    const imgs = tile.querySelectorAll("img");
    for (const img of imgs) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      const alt = (img.getAttribute("alt") || "").toLowerCase();
      if (isFlowGeneratedImageSrc(src)) return img;
      if (/imagem gerada|generated image/i.test(alt) && src) return img;
    }
    return null;
  }

  function findGeneratedImage(baselineSrcs, baselineTileIds) {
    for (const tile of document.querySelectorAll("[data-tile-id]")) {
      const tileId = tile.getAttribute("data-tile-id");
      if (!tileId || baselineTileIds.has(tileId)) continue;
      const img = findImageInTile(tile);
      if (img) return img;
    }

    const fresh = collectFlowGeneratedImages().filter((img) => {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      return src && !baselineSrcs.has(src);
    });
    if (fresh.length) return pickBestImage(fresh);

    for (const tile of document.querySelectorAll("[data-tile-id]")) {
      if (baselineTileIds.has(tile.getAttribute("data-tile-id"))) continue;
      const img = tile.querySelector("img");
      if (img && !img.closest("#flow-desktop-header")) return img;
    }

    return null;
  }

  async function maybeRetrySubmit(job) {
    if (job.submitAttempts >= 2) return;
    const elapsed = Date.now() - job.startedAt;
    if (elapsed < 20000 || elapsed - job.lastSubmitAt < 20000) return;

    const img = findGeneratedImage(job.baselineSrcs, job.baselineTileIds);
    if (img) return;
    if (isGenerating()) return;
    if (!getPromptText()) return;

    job.submitAttempts += 1;
    job.lastSubmitAt = Date.now();
    await ext.storage.local.set({
      [RUN_STORAGE.step]: `2/3 — Reenviando prompt no Google Flow (tentativa ${job.submitAttempts + 1})…`,
    });
    await submitPrompt(job.baselineTileIds, job.baselineSrcs);
  }

  async function tickImageWait(job) {
    const elapsed = Math.round((Date.now() - job.startedAt) / 1000);
    if (elapsed > 0 && Math.floor(elapsed) % 5 === 0 && job.step !== `t${Math.floor(elapsed)}`) {
      job.step = `t${Math.floor(elapsed)}`;
      const msg = job.manualWait 
        ? `2/3 — Aguardando clique manual no Google Flow… (${Math.floor(elapsed)}s)`
        : `2/3 — Aguardando imagem no Google Flow… (${Math.floor(elapsed)}s)`;
      await ext.storage.local.set({
        [RUN_STORAGE.step]: msg,
      });
    }

    await maybeRetrySubmit(job);

    const generating = isGenerating();
    const img = findGeneratedImage(job.baselineSrcs, job.baselineTileIds);

    if (img) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (src === job.lastSrc) {
        job.imageStableMs += 500;
      } else {
        job.lastSrc = src;
        job.imageStableMs = 0;
      }

      if (job.imageStableMs >= 1000) {
        try {
          const dataUrl = await captureImageAsDataUrl(img);
          if (typeof dataUrl === "string" && dataUrl.startsWith("data:image/")) {
            job.bestDataUrl = dataUrl;
          }
          const ready =
            (!generating && job.imageStableMs >= 2000) || job.imageStableMs >= 4500;
          if (ready) return { done: true, imageDataUrl: dataUrl };
        } catch {
          /* ainda carregando */
        }
      }
    } else {
      job.lastSrc = "";
      job.imageStableMs = 0;
    }

    if (Date.now() - job.startedAt > IMAGE_TIMEOUT_MS) {
      if (job.bestDataUrl) return { done: true, imageDataUrl: job.bestDataUrl };
      throw new Error(
        "Imagem não capturada no Google Flow. Verifique login, cota e se a geração terminou."
      );
    }
    return { done: false };
  }

  async function workflowTick() {
    if (!workflowJob) return { active: false };
    await throwIfAborted();

    try {
      const result = await tickImageWait(workflowJob);
      if (result.done) {
        const imageDataUrl = result.imageDataUrl;
        workflowJob = null;
        await notifyWorkflowStepDone("RUN_IMAGE", { ok: true, imageDataUrl });
        return { active: false, completed: true };
      }
    } catch (err) {
      workflowJob = null;
      const error = err?.message || String(err);
      await notifyWorkflowStepDone("RUN_IMAGE", { ok: false, error });
      throw err;
    }
    return { active: true };
  }

  async function startImageJob(prompt) {
    abortRequested = false;
    workflowJob = null;
    await ext.storage.local.set({ [RUN_STORAGE.cancelRequested]: false });

    await waitForFlowProject();

    const blocked = isBlockedOrLoginPage();
    if (blocked) throw new Error(blocked);

    await trySetImageMode();
    await trySetAspectRatio916();

    const baselineSrcs = snapshotImageSrcs();
    const baselineTileIds = snapshotTileIds();
    await waitForPromptInput();
    await closeComposerDialogs();

    let submitted = false;
    let manualWait = false;

    for (let attempt = 0; attempt < 2; attempt += 1) {
      let fillResult = await requestFlowPageBridge("fillPrompt", { prompt });
      if (!fillResult?.submitReady) {
        if (!fillResult?.filled) {
          const input = findPromptInputSync();
          if (input) fillPrompt(input, prompt);
          await sleep(500);
        }
        if (!isSubmitButtonEnabled(findSubmitButton())) {
          fillResult = await requestFlowPageBridge("fillPrompt", { prompt });
        }
      }

      const enabledBtn = await waitForSubmitButtonEnabled(15000);
      if (enabledBtn) {
        await closeComposerDialogs();
        submitted = await submitPrompt(baselineTileIds, baselineSrcs);
        if (submitted) break;
      }
      
      await sleep(1000);
    }

    if (!submitted) {
      console.warn("Falha ao submeter prompt automaticamente. Aguardando clique manual do usuário.");
      manualWait = true;
    }

    workflowJob = {
      startedAt: Date.now(),
      lastSrc: "",
      imageStableMs: 0,
      bestDataUrl: "",
      baselineSrcs,
      baselineTileIds,
      submitAttempts: 0,
      lastSubmitAt: Date.now(),
      step: "",
      manualWait,
    };
  }

  if (isLabsFlowUrl(window.location.href)) {
    persistFlowProjectUrl();
  }

  ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.action === "PING") {
      sendResponse({ ok: true, flow: isLabsFlowUrl(window.location.href) });
      return false;
    }

    if (message?.action === "ABORT") {
      abortRequested = true;
      sendResponse({ ok: true });
      return false;
    }

    if (message?.action === "WORKFLOW_TICK") {
      workflowTick()
        .then((state) => sendResponse({ ok: true, ...state }))
        .catch(async (err) => {
          workflowJob = null;
          const error = err?.message || String(err);
          sendResponse({ ok: false, error });
        });
      return true;
    }

    if (message?.action === "RUN_IMAGE") {
      startImageJob(message.prompt || "")
        .then(() => sendResponse({ ok: true, pending: true }))
        .catch(async (err) => {
          workflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("RUN_IMAGE", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    return false;
  });
}
