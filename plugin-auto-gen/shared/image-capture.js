/* global IMAGE_TIMEOUT_MS */

globalThis.IMAGE_TIMEOUT_MS = 600000;

globalThis.isFlowGeneratedImageSrc = function isFlowGeneratedImageSrc(src) {
  if (!src) return false;
  return src.includes("media.getMediaUrlRedirect");
};

globalThis.isExcludedFlowImage = function isExcludedFlowImage(img) {
  if (!img) return true;
  const src = (img.currentSrc || img.src || img.getAttribute("data-src") || "").toLowerCase();
  const alt = (img.getAttribute("alt") || "").toLowerCase();
  if (img.closest?.("#flow-desktop-header")) return true;
  if (/\/fx\/icons\//i.test(src)) return true;
  if (/perfil|profile|avatar|user photo/i.test(alt)) return true;
  const w = img.naturalWidth || img.width || 0;
  const h = img.naturalHeight || img.height || 0;
  if (src.includes("googleusercontent") && (w <= 128 || h <= 128)) return true;
  return false;
};

globalThis.isLikelyGeneratedImageSrc = function isLikelyGeneratedImageSrc(src) {
  if (!src) return false;
  const s = src.toLowerCase();
  if (s.startsWith("data:image/svg")) return false;
  if (globalThis.isFlowGeneratedImageSrc(s)) return true;
  if (s.includes("avatar") || s.includes("favicon") || s.includes("emoji")) return false;
  if (s.includes("/fx/icons/")) return false;
  if (s.includes("logo") && s.length < 120) return false;
  return (
    s.startsWith("blob:") ||
    s.startsWith("data:image/") ||
    s.includes("googleusercontent") ||
    s.includes("ggpht") ||
    s.includes("gstatic") ||
    s.includes("oaidalle") ||
    s.includes("oaiusercontent") ||
    s.includes("openai") ||
    s.includes("chatgpt") ||
    s.includes("oaistatic") ||
    s.includes("cdn.") ||
    s.includes("images.") ||
    /\.(png|jpe?g|webp|gif)(\?|$)/i.test(s)
  );
};

globalThis.isFlowGeneratedImage = function isFlowGeneratedImage(img) {
  if (!img || globalThis.isExcludedFlowImage(img)) return false;
  const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
  if (globalThis.isFlowGeneratedImageSrc(src)) return true;
  const alt = (img.getAttribute("alt") || "").toLowerCase();
  if (/imagem gerada|generated image/i.test(alt)) return true;
  if (!globalThis.isLikelyGeneratedImageSrc(src)) return false;
  const w = img.naturalWidth || img.width || 0;
  const h = img.naturalHeight || img.height || 0;
  return w >= 64 && h >= 64;
};

globalThis.waitForImageElementLoad = async function waitForImageElementLoad(img, timeoutMs = 30000) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    if (img.complete && w > 0 && h > 0) return;
    await sleep(200);
  }
};

globalThis.imgToDataUrlViaCanvas = async function imgToDataUrlViaCanvas(img) {
  await globalThis.waitForImageElementLoad(img);
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth || img.width;
  canvas.height = img.naturalHeight || img.height;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0);
  return canvas.toDataURL("image/png");
};

globalThis.urlToDataUrl = async function urlToDataUrl(url) {
  const absolute = url.startsWith("http") ? url : new URL(url, window.location.origin).href;
  const response = await fetch(absolute);
  const blob = await response.blob();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Falha ao ler imagem."));
    reader.readAsDataURL(blob);
  });
};

globalThis.captureImageAsDataUrl = async function captureImageAsDataUrl(img) {
  const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
  if (!src) throw new Error("Imagem sem URL.");

  if (src.startsWith("data:image/")) return src;

  try {
    return await globalThis.urlToDataUrl(src);
  } catch {
    /* canvas fallback */
  }

  return globalThis.imgToDataUrlViaCanvas(img);
};

globalThis.getFlowImageSearchRoot = function getFlowImageSearchRoot() {
  const tileRoot = document.querySelector("#__next [data-tile-id]");
  if (tileRoot?.parentElement) return tileRoot.parentElement;
  const next = document.querySelector("#__next");
  if (!next) return document.body;
  const header = next.querySelector("#flow-desktop-header");
  if (!header) return next;
  const clone = next.cloneNode(true);
  clone.querySelector("#flow-desktop-header")?.remove();
  return clone;
};

globalThis.collectImagesFromNode = function collectImagesFromNode(root) {
  if (!root) return [];
  const found = [];

  root.querySelectorAll("img").forEach((img) => {
    if (!globalThis.isFlowGeneratedImage(img)) return;
    const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
    if (globalThis.isFlowGeneratedImageSrc(src)) {
      found.push(img);
      return;
    }
    const w = img.naturalWidth || img.width || 0;
    const h = img.naturalHeight || img.height || 0;
    if (w < 64 || h < 64) return;
    found.push(img);
  });

  root.querySelectorAll("[style*='background-image']").forEach((el) => {
    const match = (el.style.backgroundImage || "").match(/url\(["']?([^"')]+)/);
    if (match && globalThis.isLikelyGeneratedImageSrc(match[1])) {
      const proxy = document.createElement("img");
      proxy.src = match[1];
      if (!globalThis.isExcludedFlowImage(proxy)) found.push(proxy);
    }
  });

  return found;
};

globalThis.collectFlowGeneratedImages = function collectFlowGeneratedImages() {
  const roots = [];
  const tileParent = document.querySelector("#__next [data-tile-id]")?.closest("div");
  if (tileParent) roots.push(tileParent);
  const next = document.querySelector("#__next");
  if (next) {
    next.querySelectorAll("[data-tile-id]").forEach((tile) => {
      if (tile) roots.push(tile);
    });
  }
  if (!roots.length && next) roots.push(next);

  const seen = new Set();
  const found = [];
  for (const root of roots) {
    if (root.closest?.("#flow-desktop-header")) continue;
    for (const img of globalThis.collectImagesFromNode(root)) {
      const key = img.currentSrc || img.src || "";
      if (!key || seen.has(key)) continue;
      seen.add(key);
      found.push(img);
    }
  }
  return found;
};

globalThis.pickBestImage = function pickBestImage(images) {
  if (!images.length) return null;
  return images.reduce((best, img) => {
    const area = (img.naturalWidth || img.width || 0) * (img.naturalHeight || img.height || 0);
    const bestArea =
      (best.naturalWidth || best.width || 0) * (best.naturalHeight || best.height || 0);
    return area >= bestArea ? img : best;
  });
};
