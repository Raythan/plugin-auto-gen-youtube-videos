/* Pure DOM helpers for Google Flow composer — no extension APIs. */

globalThis.getFlowButtonIcon = function getFlowButtonIcon(btn) {
  if (!btn) return "";
  const icon = btn.querySelector("i.google-symbols, i.material-symbols");
  return (icon?.textContent || "").trim();
};

globalThis.isFlowAddMenuButton = function isFlowAddMenuButton(btn) {
  if (!btn) return false;
  if (getFlowButtonIcon(btn) === "add_2") return true;
  return btn.getAttribute("aria-haspopup") === "dialog" && getFlowButtonIcon(btn) === "add_2";
};

globalThis.findFlowComposerRoot = function findFlowComposerRoot(doc = document) {
  const slate = doc.querySelector(
    '[role="textbox"][data-slate-editor="true"][contenteditable="true"]'
  );
  if (!slate) return null;
  return (
    slate.closest("[class*='sc-26b30722']") ||
    slate.closest("[class*='sc-682f0b3f']") ||
    slate.closest(".sc-682f0b3f-1")
  );
};

globalThis.findFlowAddMenuButton = function findFlowAddMenuButton(root = null) {
  const scope = root || findFlowComposerRoot() || document;
  for (const btn of scope.querySelectorAll("button[aria-haspopup='dialog']")) {
    if (isFlowAddMenuButton(btn)) return btn;
  }
  for (const icon of scope.querySelectorAll("i.google-symbols, i.material-symbols")) {
    if ((icon.textContent || "").trim() !== "add_2") continue;
    const btn = icon.closest("button");
    if (btn && isFlowAddMenuButton(btn)) return btn;
  }
  return null;
};

globalThis.isFlowSubmitButton = function isFlowSubmitButton(btn) {
  if (!btn || btn.tagName !== "BUTTON") return false;
  if (btn.getAttribute("aria-haspopup")) return false;
  if (isFlowAddMenuButton(btn)) return false;
  if (getFlowButtonIcon(btn) === "add_2") return false;
  if (getFlowButtonIcon(btn) !== "arrow_forward") return false;
  if (btn.closest("#flow-desktop-header")) return false;
  return true;
};

globalThis.findFlowSubmitButton = function findFlowSubmitButton(root = null) {
  const composer = root || findFlowComposerRoot() || document;
  const submitRow = composer.querySelector("[class*='sc-26b30722-10']");

  if (submitRow) {
    const buttons = submitRow.querySelectorAll("button:not([aria-haspopup])");
    for (const btn of buttons) {
      if (isFlowSubmitButton(btn)) return btn;
    }

    for (const icon of submitRow.querySelectorAll("i.google-symbols, i.material-symbols")) {
      if ((icon.textContent || "").trim() !== "arrow_forward") continue;
      const btn = icon.closest("button");
      if (btn && isFlowSubmitButton(btn)) return btn;
    }
  }

  const scopes = root ? [composer] : [composer, document];
  for (const scope of scopes) {
    for (const icon of scope.querySelectorAll("i.google-symbols, i.material-symbols")) {
      if ((icon.textContent || "").trim() !== "arrow_forward") continue;
      const btn = icon.closest("button");
      if (btn && isFlowSubmitButton(btn)) return btn;
    }
  }
  return null;
};

globalThis.isFlowSubmitButtonEnabled = function isFlowSubmitButtonEnabled(btn) {
  return !!(btn && btn.getAttribute("aria-disabled") !== "true" && !btn.disabled);
};

globalThis.findFlowPromptInput = function findFlowPromptInput(doc = document) {
  return doc.querySelector(
    '[role="textbox"][data-slate-editor="true"][contenteditable="true"]'
  );
};

globalThis.isFlowAddMenuOpen = function isFlowAddMenuOpen(root = null) {
  const addBtn = findFlowAddMenuButton(root);
  return addBtn?.getAttribute("aria-expanded") === "true";
};
