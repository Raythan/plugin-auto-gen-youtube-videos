/* global RUN_STORAGE, RUN_STATUS, ENGINE_STORAGE, parseWorkflowJson, writePageFormBackup, readPageFormBackup */

const ext = typeof browser !== "undefined" ? browser : chrome;

if (!globalThis.__chatgptRunnerBootstrapped) {
  globalThis.__chatgptRunnerBootstrapped = true;

  let abortRequested = false;
  /** @type {null | { wait: 'text', messagesBefore: number, startedAt: number, lastText: string, stableMs: number, step: string, requireJson: boolean }} */
  let workflowJob = null;

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

  function queryFirst(selectors, root = document) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      if (el) return el;
    }
    return null;
  }

  function queryButtonByText(texts) {
    const normalized = texts.map((t) => t.toLowerCase());
    const buttons = document.querySelectorAll("button, a");
    for (const btn of buttons) {
      const label = (btn.getAttribute("aria-label") || btn.textContent || "")
        .trim()
        .toLowerCase();
      if (normalized.some((t) => label.includes(t))) return btn;
    }
    return null;
  }

  function isLoginPage() {
    if (/auth|login|signin/i.test(location.href)) return true;
    const hasPrompt = queryFirst([
      "#prompt-textarea",
      "textarea#prompt-textarea",
      "div#prompt-textarea",
      "div[contenteditable='true'][id*='prompt']",
    ]);
    return !hasPrompt && !!queryButtonByText(["log in", "entrar", "sign in"]);
  }

  async function waitForPromptInput(timeoutMs = 90000) {
    const selectors = [
      "#prompt-textarea",
      "textarea#prompt-textarea",
      "div#prompt-textarea",
      "div[contenteditable='true']#prompt-textarea",
      "div[contenteditable='true'][id*='prompt']",
      "textarea[data-testid='prompt-textarea']",
      "div.ProseMirror[contenteditable='true']",
    ];
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await throwIfAborted();
      const el = queryFirst(selectors);
      if (el) return el;
      await sleep(400);
    }
    throw new Error("Campo de prompt não encontrado. Faça login no ChatGPT.");
  }

  async function clickNewChat() {
    const bySelector = queryFirst([
      "[data-testid='create-new-chat-button']",
      "a[data-testid='create-new-chat-button']",
      "nav a[href='/']",
    ]);
    if (bySelector) {
      bySelector.click();
      await sleep(1500);
      return;
    }

    const byText = queryButtonByText(["new chat", "novo chat", "nova conversa"]);
    if (byText) {
      byText.click();
      await sleep(1500);
      return;
    }

    if (location.pathname !== "/") {
      location.href = "https://chatgpt.com/";
      await sleep(2500);
    }
  }

  function fillPrompt(element, value) {
    element.focus();

    if (element instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        "value"
      )?.set;
      setter?.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }

    if (element.isContentEditable) {
      element.textContent = "";
      if (document.execCommand) {
        document.execCommand("selectAll", false, null);
        document.execCommand("insertText", false, value);
      } else {
        element.textContent = value;
      }
      element.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "insertText", data: value })
      );
      return;
    }

    element.textContent = value;
    element.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function submitPrompt(input) {
    await sleep(200);
    const sendBtn = queryFirst([
      "button[data-testid='send-button']",
      "button[data-testid='composer-send-button']",
      "button[aria-label*='Send' i]",
      "button[aria-label*='Enviar' i]",
    ]);

    if (sendBtn && !sendBtn.disabled) {
      sendBtn.click();
      return;
    }

    input.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
      })
    );
  }

  function getAssistantMessages() {
    return Array.from(document.querySelectorAll("[data-message-author-role='assistant']"));
  }

  function extractMessageText(node) {
    if (!node) return "";
    const markdown = node.querySelector(".markdown, .prose, [class*='markdown']");
    const source = markdown || node;

    const codeBlocks = source.querySelectorAll("pre code, code");
    if (codeBlocks.length) {
      const texts = Array.from(codeBlocks)
        .map((el) => (el.textContent || "").trim())
        .filter(Boolean);
      const withKeys = texts.find((t) => /roteiro_post|prompt_imagem/i.test(t));
      if (withKeys) return withKeys;
      if (texts.length === 1) return texts[0];
    }

    return (source.textContent || source.innerText || "").trim();
  }

  function isGenerating() {
    return !!queryFirst([
      "button[data-testid='stop-button']",
      "button[aria-label*='Stop' i]",
      "button[aria-label*='Parar' i]",
    ]);
  }

  const TEXT_STABLE_THRESHOLD = 2500;
  const TEXT_TIMEOUT_MS = 300000;

  function tickTextWait(job) {
    const generating = isGenerating();
    const messages = getAssistantMessages();
    const slice =
      messages.length > job.messagesBefore ? messages.slice(job.messagesBefore) : messages;
    const last = slice[slice.length - 1] || messages[messages.length - 1];
    const text = extractMessageText(last);

    if (!generating && text.length > 0) {
      if (text === job.lastText) {
        job.stableMs += 500;
        if (job.stableMs >= TEXT_STABLE_THRESHOLD) {
          if (job.requireJson) {
            try {
              parseWorkflowJson(text);
            } catch {
              const nearTimeout = Date.now() - job.startedAt > TEXT_TIMEOUT_MS - 10000;
              if (!nearTimeout) return { done: false };
            }
          }
          return { done: true, response: text };
        }
      } else {
        job.lastText = text;
        job.stableMs = 0;
      }
    } else if (text) {
      job.lastText = text;
      job.stableMs = 0;
    }

    if (Date.now() - job.startedAt > TEXT_TIMEOUT_MS) {
      if (job.lastText) return { done: true, response: job.lastText };
      throw new Error("Tempo esgotado aguardando resposta de texto.");
    }
    return { done: false };
  }

  async function workflowTick() {
    if (!workflowJob) return { active: false };
    await throwIfAborted();

    if (workflowJob.wait === "text") {
      try {
        const result = tickTextWait(workflowJob);
        if (result.done) {
          const response = result.response;
          workflowJob = null;
          await notifyWorkflowStepDone("RUN_TEXT", { ok: true, response });
          return { active: false, completed: true };
        }
      } catch (err) {
        workflowJob = null;
        const error = err?.message || String(err);
        await notifyWorkflowStepDone("RUN_TEXT", { ok: false, error });
        throw err;
      }
      return { active: true };
    }

    return { active: false };
  }

  async function startTextJob(prompt, newChat, requireJson = false) {
    abortRequested = false;
    workflowJob = null;
    await ext.storage.local.set({ [RUN_STORAGE.cancelRequested]: false });

    if (isLoginPage()) {
      throw new Error("Faça login no ChatGPT e tente novamente.");
    }

    const messagesBefore = getAssistantMessages().length;

    if (newChat) {
      await throwIfAborted();
      await clickNewChat();
    }

    await throwIfAborted();
    const input = await waitForPromptInput();
    fillPrompt(input, prompt);
    await sleep(400);
    await submitPrompt(input);

    workflowJob = {
      wait: "text",
      messagesBefore,
      startedAt: Date.now(),
      lastText: "",
      stableMs: 0,
      step: "",
      requireJson,
    };
  }

  ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.action === "PING") {
      sendResponse({ ok: true });
      return false;
    }

    if (message?.action === "SAVE_FORM_BACKUP") {
      writePageFormBackup(message.data || {});
      sendResponse({ ok: true });
      return false;
    }

    if (message?.action === "GET_FORM_BACKUP") {
      sendResponse({ ok: true, data: readPageFormBackup() });
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

    if (message?.action === "RUN_TEXT") {
      startTextJob(message.prompt || "", message.newChat !== false, message.requireJson === true)
        .then(() => sendResponse({ ok: true, pending: true }))
        .catch(async (err) => {
          workflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("RUN_TEXT", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    return false;
  });
}
