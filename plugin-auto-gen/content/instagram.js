/* global RUN_STORAGE, RUN_STATUS, ENGINE_STORAGE */

const ext = typeof browser !== "undefined" ? browser : chrome;

if (!globalThis.__instagramRunnerBootstrapped) {
  globalThis.__instagramRunnerBootstrapped = true;

  let abortRequested = false;
  /** @type {null | { text: string, imageDataUrl: string, stage: number, advanceAttempts?: number }} */
  let igWorkflowJob = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function notifyWorkflowStepDone(step, data) {
    const result = { step, at: Date.now(), ...data };
    await ext.storage.local.set({ [ENGINE_STORAGE.stepResult]: result });
    try {
      await ext.runtime.sendMessage({ action: "WORKFLOW_STEP_DONE", ...result });
    } catch {
      /* background suspenso: keepalive lê o storage */
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

  function getDialogRoots() {
    const roots = [
      ...document.querySelectorAll('[role="dialog"]'),
      ...document.querySelectorAll('div[aria-modal="true"]'),
    ];
    return roots.length ? roots : [document];
  }

  function queryFirst(selectors, roots = getDialogRoots()) {
    for (const root of roots) {
      for (const selector of selectors) {
        const el = root.querySelector(selector);
        if (el) return el;
      }
    }
    return null;
  }

  function queryByText(texts, roots = getDialogRoots()) {
    const normalized = texts.map((t) => t.toLowerCase());
    for (const root of roots) {
      const nodes = root.querySelectorAll(
        "button, a, div[role='button'], span[role='button'], [role='menuitem']"
      );
      for (const node of nodes) {
        const label = (
          node.getAttribute("aria-label") ||
          node.getAttribute("title") ||
          node.textContent ||
          ""
        )
          .trim()
          .toLowerCase();
        if (!label) continue;
        if (normalized.some((t) => label === t || label.includes(t))) return node;
      }
    }
    return null;
  }

  function setInputValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
      "value"
    )?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function readCaptionFromElement(el) {
    if (!el) return "";
    if (el instanceof HTMLTextAreaElement) return (el.value || "").trim();
    return (el.textContent || el.innerText || "").trim();
  }

  function isCaptionFilled(el, expected) {
    const sample = String(expected || "").trim().slice(0, Math.min(40, String(expected || "").length));
    if (!sample) return false;
    const actual = readCaptionFromElement(el);
    return actual.includes(sample);
  }

  function dispatchPasteEvent(el, text) {
    el.focus();
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    const pasteEvent = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dt,
    });
    return el.dispatchEvent(pasteEvent);
  }

  function dispatchInsertFromPaste(el, text) {
    el.focus();
    try {
      document.execCommand("selectAll", false, null);
    } catch {
      /* ignore */
    }
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

  async function tryClipboardPaste(el, text) {
    try {
      await navigator.clipboard.writeText(text);
      el.focus();
      el.click();
      await sleep(150);
      if (document.execCommand("paste")) return true;
    } catch {
      /* clipboard indisponível */
    }
    return dispatchPasteEvent(el, text);
  }

  async function fillCaptionField(el, text) {
    el.focus();
    el.click();
    await sleep(200);

    if (el instanceof HTMLTextAreaElement) {
      setInputValue(el, text);
      if (isCaptionFilled(el, text)) return;
    }

    await tryClipboardPaste(el, text);
    await sleep(400);
    if (isCaptionFilled(el, text)) return;

    dispatchInsertFromPaste(el, text);
    await sleep(400);
    if (isCaptionFilled(el, text)) return;

    try {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
    } catch {
      el.textContent = text;
      el.dispatchEvent(
        new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" })
      );
    }
    await sleep(400);
    if (!isCaptionFilled(el, text)) {
      throw new Error(
        "Legenda não foi aplicada no campo do Instagram (editor Lexical rejeitou o texto)."
      );
    }
  }

  function findCaptionField() {
    const specific = queryFirst([
      'textarea[aria-label^="Write a caption"]',
      'div[aria-label^="Write a caption"][contenteditable="true"]',
      'textarea[aria-label^="Escreva uma legenda"]',
      'div[aria-label^="Escreva uma legenda"][contenteditable="true"]',
      'textarea[aria-label*="caption" i]',
      'textarea[aria-label*="legenda" i]',
      'div[contenteditable="true"][aria-label*="caption" i]',
      'div[contenteditable="true"][aria-label*="legenda" i]',
      'div[contenteditable="true"][data-lexical-editor="true"][role="textbox"]',
      'div[contenteditable="true"][data-lexical-editor="true"]',
    ]);
    if (specific) return specific;

    const roots = getDialogRoots();
    for (const root of roots) {
      if (root === document) continue;
      const inDialog = queryFirst(
        [
          'textarea[aria-label*="caption" i]',
          'textarea[aria-label*="legenda" i]',
          'div[contenteditable="true"][aria-label*="caption" i]',
          'div[contenteditable="true"][aria-label*="legenda" i]',
          'div[contenteditable="true"][data-lexical-editor="true"]',
          "textarea",
        ],
        [root]
      );
      if (inDialog) return inDialog;
    }

    return null;
  }

  function dataUrlToFile(dataUrl, filename = "post-image.png") {
    const [header, b64] = dataUrl.split(",");
    const mime = (header.match(/:(.*?);/) || [])[1] || "image/png";
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new File([bytes], filename, { type: mime });
  }

  function setFilesOnInput(input, file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function openCreatePostDialog() {
    if (/\/create\//i.test(location.pathname)) {
      await sleep(1500);
      return true;
    }

    const createBtn = queryByText([
      "new post",
      "nova publicação",
      "criar",
      "create",
      "novo post",
    ], [document]);
    if (createBtn) {
      createBtn.click();
      await sleep(2000);
      return true;
    }

    const createLink = queryFirst(
      [
        'a[href="#"][role="link"] svg[aria-label*="New post" i]',
        'a[href="#"] svg[aria-label*="Nova publicação" i]',
        'svg[aria-label="New post"]',
        'svg[aria-label="Nova publicação"]',
        'svg[aria-label="Criar"]',
      ],
      [document]
    );
    if (createLink) {
      const clickable = createLink.closest("a, button, div[role='button']") || createLink;
      clickable.click();
      await sleep(2000);
      return true;
    }

    return false;
  }

  async function uploadImage(imageDataUrl) {
    const file = dataUrlToFile(imageDataUrl);

    for (let attempt = 0; attempt < 25; attempt += 1) {
      await throwIfAborted();
      const fileInput = queryFirst([
        'input[type="file"][accept*="image"]',
        'input[type="file"][accept*="video"]',
        'input[type="file"]',
      ]);
      if (fileInput) {
        setFilesOnInput(fileInput, file);
        await sleep(4000);
        return;
      }

      if (attempt % 4 === 0) {
        const selectFromComputer = queryByText([
          "select from computer",
          "selecionar do computador",
          "selecionar no computador",
        ]);
        if (selectFromComputer) {
          selectFromComputer.click();
          await sleep(1000);
        }
      }

      await sleep(600);
    }

    throw new Error("Campo de upload de imagem não encontrado no Instagram.");
  }

  async function clickNextIfPresent() {
    const nextBtn = queryByText(["next", "avançar", "próximo", "proximo"]);
    if (nextBtn) {
      nextBtn.click();
      await sleep(2500);
      return true;
    }

    const headerNext = queryFirst([
      'div[role="dialog"] div[role="button"]',
      'div[aria-modal="true"] div[role="button"]',
    ]);
    if (headerNext) {
      const label = (
        headerNext.getAttribute("aria-label") ||
        headerNext.textContent ||
        ""
      ).toLowerCase();
      if (label.includes("next") || label.includes("avançar") || label.includes("proximo")) {
        headerNext.click();
        await sleep(2500);
        return true;
      }
    }

    return false;
  }

  async function waitForCaptionScreen(job) {
    job.advanceAttempts = (job.advanceAttempts || 0) + 1;

    const captionField = findCaptionField();
    if (captionField) return true;

    if (job.advanceAttempts > 30) {
      throw new Error(
        "Campo de legenda não encontrado no Instagram. Verifique se o diálogo de criação abriu."
      );
    }

    await clickNextIfPresent();
    return false;
  }

  async function fillCaption(text) {
    let lastError = "";

    for (let attempt = 0; attempt < 25; attempt += 1) {
      await throwIfAborted();
      const el = findCaptionField();
      if (el) {
        try {
          await fillCaptionField(el, text);
          await sleep(500);
          return;
        } catch (err) {
          lastError = err?.message || String(err);
          await sleep(800);
        }
      }

      if (attempt % 5 === 4) {
        await clickNextIfPresent();
      }

      await sleep(600);
    }

    throw new Error(
      lastError || "Campo de legenda não encontrado no Instagram."
    );
  }

  async function submitPost() {
    await sleep(1000);
    const shareBtn = queryByText(["share", "compartilhar", "publicar"]);
    if (shareBtn) {
      shareBtn.click();
      await sleep(5000);
      return;
    }

    throw new Error('Botão "Compartilhar" não encontrado no Instagram.');
  }

  async function startCreatePostJob(text, imageDataUrl) {
    if (!text?.trim()) throw new Error("roteiro_post vazio.");
    if (!imageDataUrl?.startsWith("data:image")) {
      throw new Error("Imagem inválida para o post.");
    }
    if (!/instagram\.com/i.test(location.href)) {
      throw new Error("Abra o Instagram logado em instagram.com.");
    }
    igWorkflowJob = { text, imageDataUrl, stage: 0, advanceAttempts: 0 };
  }

  async function workflowTickInstagram() {
    if (!igWorkflowJob) return { active: false };
    await throwIfAborted();

    const job = igWorkflowJob;
    if (job.stage === 0) {
      await openCreatePostDialog();
      job.stage = 1;
      return { active: true };
    }
    if (job.stage === 1) {
      await uploadImage(job.imageDataUrl);
      job.stage = 2;
      return { active: true };
    }
    if (job.stage === 2) {
      const ready = await waitForCaptionScreen(job);
      if (ready) job.stage = 3;
      return { active: true };
    }
    if (job.stage === 3) {
      await fillCaption(job.text);
      job.stage = 4;
      return { active: true };
    }
    if (job.stage === 4) {
      await submitPost();
      igWorkflowJob = null;
      await notifyWorkflowStepDone("CREATE_INSTAGRAM_POST", {
        ok: true,
        posted: true,
      });
      return { active: false, completed: true };
    }
    return { active: false };
  }

  ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.action === "PING") {
      sendResponse({ ok: true });
      return false;
    }

    if (message?.action === "ABORT") {
      abortRequested = true;
      sendResponse({ ok: true });
      return false;
    }

    if (message?.action === "WORKFLOW_TICK") {
      workflowTickInstagram()
        .then((state) => sendResponse({ ok: true, ...state }))
        .catch(async (err) => {
          igWorkflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("CREATE_INSTAGRAM_POST", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    if (message?.action === "CREATE_INSTAGRAM_POST") {
      startCreatePostJob(message.text, message.imageDataUrl)
        .then(() => sendResponse({ ok: true, pending: true }))
        .catch(async (err) => {
          igWorkflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("CREATE_INSTAGRAM_POST", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    return false;
  });
}
