/* global RUN_STORAGE */

globalThis.dataUrlToBlob = function dataUrlToBlob(dataUrl) {
  const parts = String(dataUrl || "").split(",");
  if (parts.length < 2) throw new Error("Data URL de imagem inválida.");
  const mimeMatch = parts[0].match(/:(.*?);/);
  const mime = mimeMatch ? mimeMatch[1] : "image/png";
  const binary = atob(parts[1]);
  const arr = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    arr[i] = binary.charCodeAt(i);
  }
  return new Blob([arr], { type: mime });
};

/**
 * @param {object} opts
 * @param {string} opts.bridgeUrl
 * @param {string} [opts.bridgeToken]
 * @param {string} opts.channelId
 * @param {object} opts.script
 * @param {string[]} opts.imagesDataUrls
 */
globalThis.exportContentPackage = async function exportContentPackage({
  bridgeUrl,
  bridgeToken,
  channelId,
  script,
  imagesDataUrls,
}) {
  const url = (bridgeUrl || "").trim().replace(/\/$/, "");
  if (!url) throw new Error("URL do bridge não configurada.");
  if (!channelId) throw new Error("ID do canal não configurado.");
  if (!script?.script_text) throw new Error("Roteiro vazio.");
  if (!imagesDataUrls?.length) throw new Error("Nenhuma imagem para exportar.");

  const manifest = {
    channel_id: channelId,
    source: "plugin-auto-gen",
    script: {
      title: script.title || "",
      script_text: script.script_text || "",
      youtube_body: script.youtube_body || script.script_text || "",
      tags: script.tags || [],
      visual_scenes: script.visual_scenes || [],
      topic_key: script.topic_key || "",
    },
  };

  const form = new FormData();
  form.append("manifest", JSON.stringify(manifest));
  imagesDataUrls.forEach((dataUrl, index) => {
    const name = `${String(index + 1).padStart(2, "0")}.png`;
    const field = `image_${String(index + 1).padStart(2, "0")}`;
    form.append(field, dataUrlToBlob(dataUrl), name);
  });

  const headers = {};
  if (bridgeToken) headers["X-Bridge-Token"] = bridgeToken;

  const response = await fetch(url, { method: "POST", body: form, headers });
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { raw: text };
  }
  if (!response.ok) {
    throw new Error(payload.error || payload.raw || `Bridge HTTP ${response.status}`);
  }
  return payload;
};

globalThis.checkBridgeHealth = async function checkBridgeHealth(bridgeUrl) {
  const base = (bridgeUrl || "").trim().replace(/\/content\/?$/, "").replace(/\/$/, "");
  if (!base) return false;
  try {
    const response = await fetch(`${base}/health`, { method: "GET" });
    if (!response.ok) return false;
    const data = await response.json();
    return data?.status === "ok";
  } catch {
    return false;
  }
};
