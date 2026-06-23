/* global findFlowSubmitButton, findFlowPromptInput, isFlowSubmitButton, isFlowSubmitButtonEnabled, isFlowAddMenuOpen */

/* Google Flow — MAIN world: preenchimento e clique como usuário real. */
(function () {
  if (globalThis.__labsFlowPageBootstrapped) return;
  globalThis.__labsFlowPageBootstrapped = true;

  const SUBMIT_EVENT = "plugin-auto-gen-flow-submit";
  const RESULT_EVENT = "plugin-auto-gen-flow-submit-result";
  const HOVER_MS = 180;
  const TYPE_DELAY_MS = 8;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function synthPointer(type, el, cx, cy) {
    const base = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window };
    if (type.startsWith("pointer") || type === "click") {
      return new PointerEvent(type, {
        ...base,
        pointerId: 1,
        pointerType: "mouse",
        isPrimary: true,
        button: 0,
      });
    }
    return new MouseEvent(type, { ...base, button: 0 });
  }

  function invokeReactHandler(el, name, extra = {}) {
    if (!el) return false;
    const fakeNative = new Event(name.replace(/^on/, "").toLowerCase(), {
      bubbles: true,
      cancelable: true,
    });
    Object.defineProperty(fakeNative, "isTrusted", { get: () => true });

    const event = {
      preventDefault() {},
      stopPropagation() {},
      nativeEvent: fakeNative,
      target: el,
      currentTarget: el,
      type: name.replace(/^on/, "").toLowerCase(),
      button: 0,
      isTrusted: true,
      ...extra,
    };

    const propsKey = Object.keys(el).find((k) => k.startsWith("__reactProps$"));
    if (propsKey && typeof el[propsKey]?.[name] === "function") {
      el[propsKey][name](event);
      return true;
    }

    const fiberKey = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
    );
    let fiber = fiberKey ? el[fiberKey] : null;
    while (fiber) {
      const props = fiber.memoizedProps || fiber.pendingProps;
      if (props && typeof props[name] === "function") {
        props[name](event);
        return true;
      }
      fiber = fiber.return;
    }
    return false;
  }

  async function humanPointerClick(el) {
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;

    for (const type of ["pointerover", "pointerenter", "mouseover", "mouseenter"]) {
      el.dispatchEvent(synthPointer(type, el, cx, cy));
    }
    await sleep(HOVER_MS);

    for (const type of ["pointerdown", "mousedown"]) {
      el.dispatchEvent(synthPointer(type, el, cx, cy));
    }
    await sleep(40);
    for (const type of ["pointerup", "mouseup", "click"]) {
      el.dispatchEvent(synthPointer(type, el, cx, cy));
    }

    invokeReactHandler(el, "onPointerDown");
    invokeReactHandler(el, "onPointerUp");
    invokeReactHandler(el, "onClick");
    el.click();
  }

  function readPromptText(el) {
    return (el?.textContent || "").trim();
  }

  function promptMatches(el, expected) {
    const sample = String(expected || "").trim().slice(0, Math.min(48, String(expected || "").length));
    if (!sample) return false;
    return readPromptText(el).includes(sample);
  }

  function isSubmitReady() {
    const btn = findFlowSubmitButton();
    return isFlowSubmitButton(btn) && isFlowSubmitButtonEnabled(btn);
  }

  function dispatchPasteEvent(el, text) {
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    const event = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dt,
    });
    invokeReactHandler(el, "onPaste", { clipboardData: dt });
    return el.dispatchEvent(event);
  }

  function dispatchInsertFromPaste(el, text) {
    for (const type of ["beforeinput", "input"]) {
      el.dispatchEvent(
        new InputEvent(type, {
          bubbles: true,
          cancelable: true,
          inputType: "insertFromPaste",
          data: text,
        })
      );
      invokeReactHandler(el, type === "beforeinput" ? "onBeforeInput" : "onInput", {
        inputType: "insertFromPaste",
        data: text,
      });
    }
  }

  async function tryClipboardPaste(el, text) {
    try {
      await navigator.clipboard.writeText(text);
      el.focus();
      await sleep(120);
      if (document.execCommand("paste")) return true;
    } catch {
      /* sem gesto do usuário ou permissão */
    }
    return false;
  }

  async function typeTextCharByChar(el, text) {
    for (const char of text) {
      el.dispatchEvent(
        new KeyboardEvent("keydown", { key: char, bubbles: true, cancelable: true })
      );
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertText",
          data: char,
        })
      );
      invokeReactHandler(el, "onBeforeInput", { inputType: "insertText", data: char });
      try {
        document.execCommand("insertText", false, char);
      } catch {
        /* ignore */
      }
      el.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          inputType: "insertText",
          data: char,
        })
      );
      invokeReactHandler(el, "onInput", { inputType: "insertText", data: char });
      el.dispatchEvent(
        new KeyboardEvent("keyup", { key: char, bubbles: true, cancelable: true })
      );
      await sleep(TYPE_DELAY_MS);
    }
  }

  async function clearPromptField(el) {
    el.focus();
    await humanPointerClick(el);
    await sleep(120);
    try {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    } catch {
      el.textContent = "";
    }
    el.dispatchEvent(
      new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" })
    );
    await sleep(100);
  }

  async function fillFlowPrompt(prompt) {
    const el = findFlowPromptInput();
    if (!el) return { ok: false, reason: "no-input", submitReady: false };

    const text = String(prompt || "").trim();
    if (!text) return { ok: false, reason: "empty-prompt", submitReady: false };

    el.scrollIntoView({ block: "center", inline: "nearest" });
    await clearPromptField(el);

    const strategies = [
      async () => {
        if (!(await tryClipboardPaste(el, text))) return false;
        await sleep(350);
        return promptMatches(el, text);
      },
      async () => {
        dispatchPasteEvent(el, text);
        await sleep(350);
        return promptMatches(el, text);
      },
      async () => {
        dispatchInsertFromPaste(el, text);
        await sleep(350);
        return promptMatches(el, text);
      },
      async () => {
        await typeTextCharByChar(el, text);
        await sleep(200);
        return promptMatches(el, text);
      },
    ];

    let filled = false;
    for (const strategy of strategies) {
      if (filled) break;
      try {
        filled = await strategy();
      } catch {
        filled = false;
      }
      if (filled && isSubmitReady()) break;
    }

    const start = Date.now();
    while (Date.now() - start < 15000) {
      if (promptMatches(el, text) && isSubmitReady()) {
        return {
          ok: true,
          filled: true,
          submitReady: true,
          promptLen: readPromptText(el).length,
        };
      }
      if (promptMatches(el, text) && !isSubmitReady()) {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        invokeReactHandler(el, "onInput");
        await sleep(250);
        continue;
      }
      break;
    }

    return {
      ok: promptMatches(el, text),
      filled: promptMatches(el, text),
      submitReady: isSubmitReady(),
      promptLen: readPromptText(el).length,
      reason: promptMatches(el, text) ? "submit-not-enabled" : "fill-failed",
    };
  }

  function promptLength() {
    return readPromptText(findFlowPromptInput()).length;
  }

  function submitTookEffect(beforeLen) {
    if (isFlowAddMenuOpen()) return false;
    const btn = findFlowSubmitButton();
    if (beforeLen > 0 && promptLength() === 0) return true;
    if (beforeLen > 0 && btn?.getAttribute("aria-disabled") === "true") return true;
    return false;
  }

  async function clickArrowSubmit() {
    const btn = findFlowSubmitButton();
    if (!btn) return { ok: false, reason: "not-found", submitted: false };
    if (!isFlowSubmitButton(btn)) {
      return { ok: false, reason: "wrong-button", submitted: false, wrongButton: true };
    }
    if (!isFlowSubmitButtonEnabled(btn)) {
      return { ok: false, reason: "disabled", submitted: false };
    }

    const beforeLen = promptLength();
    btn.scrollIntoView({ block: "center", inline: "nearest" });
    await sleep(100);
    await humanPointerClick(btn);

    await sleep(400);
    if (submitTookEffect(beforeLen)) {
      return { ok: true, submitted: true, reason: "click" };
    }

    if (isFlowAddMenuOpen()) {
      return { ok: false, submitted: false, wrongButton: true, reason: "add-menu-opened" };
    }

    const slate = findFlowPromptInput();
    if (slate && beforeLen > 0) {
      slate.focus();
      for (const type of ["keydown", "keypress", "keyup"]) {
        slate.dispatchEvent(
          new KeyboardEvent(type, {
            key: "Enter",
            code: "Enter",
            keyCode: 13,
            which: 13,
            bubbles: true,
            cancelable: true,
          })
        );
      }
      await sleep(400);
      if (submitTookEffect(beforeLen)) {
        return { ok: true, submitted: true, reason: "enter" };
      }
    }

    return {
      ok: true,
      submitted: false,
      wrongButton: isFlowAddMenuOpen(),
      promptLen: promptLength(),
      reason: "no-effect",
    };
  }

  globalThis.__pluginAutoGenFlowFillPrompt = fillFlowPrompt;
  globalThis.__pluginAutoGenFlowClickSubmit = clickArrowSubmit;

  document.addEventListener(SUBMIT_EVENT, () => {
    clickArrowSubmit().then((result) => {
      document.dispatchEvent(
        new CustomEvent(RESULT_EVENT, { detail: result, bubbles: true, composed: true })
      );
    });
  });
})();
