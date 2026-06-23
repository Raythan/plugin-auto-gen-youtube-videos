/* global RUN_STORAGE, RUN_STATUS, ENGINE_STORAGE */

const ext = typeof browser !== "undefined" ? browser : chrome;

if (!globalThis.__youtubeRunnerBootstrapped) {
  globalThis.__youtubeRunnerBootstrapped = true;

  let abortRequested = false;
  /** @type {null | { text: string, imageDataUrl: string, stage: number }} */
  let ytWorkflowJob = null;

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

  function queryAll(selectors, root = document) {
    const found = [];
    for (const selector of selectors) {
      root.querySelectorAll(selector).forEach((el) => found.push(el));
    }
    return found;
  }

  function queryByText(texts, root = document) {
    const normalized = texts.map((t) => t.toLowerCase());
    const nodes = root.querySelectorAll(
      "button, a, yt-button, tp-yt-paper-button, ytd-button-renderer, div[role='button'], yt-icon-button, ytd-backstage-post-type-button-renderer"
    );
    for (const node of nodes) {
      const label = (node.getAttribute("aria-label") || node.getAttribute("title") || node.textContent || "")
        .trim()
        .toLowerCase();
      if (normalized.some((t) => label.includes(t))) return node;
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
    const createBtn = queryByText([
      "create",
      "criar",
      "write a post",
      "escrever",
      "new post",
      "novo post",
      "compor",
    ]);
    if (createBtn) {
      createBtn.click();
      await sleep(2000);
      return;
    }

    const placeholder = queryFirst([
      "div#placeholder",
      "yt-formatted-string#placeholder",
      "[placeholder*='post' i]",
      "[placeholder*='publicação' i]",
      "[aria-label*='post' i]",
      "[aria-label*='publicação' i]",
      "div#placeholder-area",
      "yt-formatted-string#commentbox-placeholder",
      "[contenteditable='true']",
      "div#contenteditable-root",
      "[id*='placeholder']",
    ]);
    if (placeholder) {
      placeholder.click();
      await sleep(1500);
    }
  }

  async function fillPostText(text) {
    const editors = [
      "div#contenteditable-root[contenteditable='true']",
      "div[contenteditable='true'][aria-label*='post' i]",
      "div[contenteditable='true'][data-placeholder]",
      "textarea[placeholder*='post' i]",
      "#contenteditable-textarea",
      "ytd-commentbox div[contenteditable='true']",
      "div[contenteditable='true']",
    ];

    for (let attempt = 0; attempt < 20; attempt += 1) {
      await throwIfAborted();
      const el = queryFirst(editors);
      if (el) {
        el.focus();
        if (el instanceof HTMLTextAreaElement) {
          setInputValue(el, text);
        } else {
          el.textContent = text;
          el.dispatchEvent(new InputEvent("input", { bubbles: true }));
        }
        await sleep(500);
        return;
      }
      
      // Se não encontrou o editor, tenta clicar no placeholder novamente
      if (attempt % 5 === 0) {
        await openCreatePostDialog();
      }
      
      await sleep(500);
    }

    throw new Error(
      "Campo de texto do post não encontrado. Abra a aba Posts do seu canal logado."
    );
  }

  async function uploadImage(imageDataUrl) {
    const file = dataUrlToFile(imageDataUrl);

    const addMedia = queryByText([
      "add image",
      "adicionar imagem",
      "image",
      "imagem",
      "photo",
      "foto",
      "media",
      "mídia",
    ]);
    
    // Tenta encontrar o botão de imagem pelo ID específico do YouTube
    const imageBtn = queryFirst(["#image-button button", "#image-button yt-icon-button", "#image-button"]);
    
    if (imageBtn) {
      imageBtn.click();
      await sleep(1000);
    } else if (addMedia) {
      addMedia.click();
      await sleep(1000);
    }

    for (let attempt = 0; attempt < 15; attempt += 1) {
      await throwIfAborted();
      const fileInput = queryFirst([
        "input[type='file'][accept*='image']",
        "input[type='file']",
      ]);
      if (fileInput) {
        setFilesOnInput(fileInput, file);
        await sleep(2500);
        return;
      }
      await sleep(500);
    }

    throw new Error("Campo de upload de imagem não encontrado na página do YouTube.");
  }

  async function openSchedulePanel() {
    const scheduleEntry = queryByText([
      "schedule",
      "agendar",
      "programar",
      "later",
      "depois",
      "publish later",
      "publicar depois",
      "publicar mais tarde",
    ]);
    if (scheduleEntry) {
      scheduleEntry.click();
      await sleep(1200);
      return true;
    }

    const postArea = queryByText(["post", "publicar"]);
    if (postArea?.parentElement) {
      const sibling = postArea.parentElement.querySelector(
        "button[aria-label*='schedule' i], button[aria-label*='agendar' i], button[aria-haspopup]"
      );
      if (sibling) {
        sibling.click();
        await sleep(800);
        const menuItem = queryByText(["schedule", "agendar", "programar"]);
        if (menuItem) {
          menuItem.click();
          await sleep(1200);
          return true;
        }
      }
    }

    const checkbox = queryFirst([
      "input[type='checkbox'][aria-label*='schedule' i]",
      "input[type='checkbox'][aria-label*='agendar' i]",
      "tp-yt-paper-checkbox[aria-label*='schedule' i]",
    ]);
    if (checkbox) {
      checkbox.click();
      await sleep(800);
      return true;
    }

    return false;
  }

  async function fillScheduleDateTime(scheduledAt) {
    const date = new Date(scheduledAt);
    const localValue = toDateTimeLocalValue(date);
    const [datePart, timePart] = localValue.split("T");

    const dtInputs = queryAll(["input[type='datetime-local']"]);
    for (const input of dtInputs) {
      setInputValue(input, localValue);
      await sleep(300);
      return true;
    }

    const dateInputs = queryAll(["input[type='date']"]);
    const timeInputs = queryAll(["input[type='time']"]);
    if (dateInputs.length && timeInputs.length) {
      setInputValue(dateInputs[0], datePart);
      setInputValue(timeInputs[0], timePart);
      await sleep(300);
      return true;
    }

    const textInputs = queryAll([
      "input[placeholder*='date' i]",
      "input[placeholder*='data' i]",
      "input[aria-label*='date' i]",
      "input[aria-label*='data' i]",
    ]);
    for (const input of textInputs) {
      setInputValue(input, date.toLocaleDateString("pt-BR"));
    }

    const timeText = queryAll([
      "input[placeholder*='time' i]",
      "input[placeholder*='hora' i]",
      "input[aria-label*='time' i]",
      "input[aria-label*='hora' i]",
    ]);
    for (const input of timeText) {
      setInputValue(
        input,
        date.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })
      );
    }

    if (textInputs.length || timeText.length) {
      await sleep(300);
      return true;
    }

    return false;
  }

  async function confirmSchedule() {
    await sleep(500);
    const confirmBtn = queryByText([
      "schedule post",
      "agendar publicação",
      "agendar post",
      "schedule",
      "agendar",
      "programar",
      "done",
      "concluir",
      "confirm",
      "confirmar",
    ]);
    if (confirmBtn) {
      confirmBtn.click();
      await sleep(2000);
      return true;
    }
    return false;
  }

  async function submitImmediatePost() {
    await sleep(800);
    const postBtn = queryByText([
      "post",
      "publicar",
      "publish",
      "enviar",
      "share",
      "compartilhar",
    ]);
    
    const submitBtn = queryFirst(["#submit-button button", "#submit-button"]);
    
    if (submitBtn && !submitBtn.disabled) {
      submitBtn.click();
      await sleep(2000);
      return;
    } else if (postBtn) {
      postBtn.click();
      await sleep(2000);
      return;
    }
    
    throw new Error('Botão "Publicar" não encontrado.');
  }

  /**
   * Tenta agendar no YouTube. Retorna false se a UI de agendamento não for encontrada.
   */
  async function tryScheduleOnYouTube(scheduledAt) {
    const opened = await openSchedulePanel();
    if (!opened) return false;

    const filled = await fillScheduleDateTime(scheduledAt);
    if (!filled) return false;

    const confirmed = await confirmSchedule();
    return confirmed;
  }

  async function startCreatePostJob(text, imageDataUrl) {
    if (!text?.trim()) throw new Error("roteiro_post vazio.");
    if (!imageDataUrl?.startsWith("data:image")) {
      throw new Error("Imagem inválida para o post.");
    }
    if (!/youtube\.com/i.test(location.href)) {
      throw new Error("Abra a página de posts do seu canal no YouTube.");
    }
    ytWorkflowJob = { text, imageDataUrl, stage: 0 };
  }

  async function workflowTickYoutube() {
    if (!ytWorkflowJob) return { active: false };
    await throwIfAborted();

    const job = ytWorkflowJob;
    if (job.stage === 0) {
      await openCreatePostDialog();
      job.stage = 1;
      return { active: true };
    }
    if (job.stage === 1) {
      await fillPostText(job.text);
      job.stage = 2;
      return { active: true };
    }
    if (job.stage === 2) {
      await uploadImage(job.imageDataUrl);
      job.stage = 3;
      return { active: true };
    }
    if (job.stage === 3) {
      await submitImmediatePost();
      ytWorkflowJob = null;
      await notifyWorkflowStepDone("CREATE_POST", {
        ok: true,
        posted: true,
        method: "immediate",
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
      workflowTickYoutube()
        .then((state) => sendResponse({ ok: true, ...state }))
        .catch(async (err) => {
          ytWorkflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("CREATE_POST", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    if (message?.action === "CREATE_POST") {
      startCreatePostJob(message.text, message.imageDataUrl)
        .then(() => sendResponse({ ok: true, pending: true }))
        .catch(async (err) => {
          ytWorkflowJob = null;
          const error = err?.message || String(err);
          await notifyWorkflowStepDone("CREATE_POST", { ok: false, error });
          sendResponse({ ok: false, error });
        });
      return true;
    }

    return false;
  });
}
