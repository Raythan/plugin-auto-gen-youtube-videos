/**
 * Extrai e valida o JSON esperado da resposta do ChatGPT.
 * @param {string} text
 * @returns {import('./parse-json-types').WorkflowScript}
 */
globalThis.parseWorkflowJson = function parseWorkflowJson(text) {
  const raw = normalizeJsonInput(text || "");
  if (!raw) {
    throw new Error("Resposta vazia do ChatGPT.");
  }

  const candidates = collectJsonCandidates(raw);
  if (!candidates.length) {
    throw new Error(
      "A resposta não é um JSON válido. Peça ao ChatGPT: title, script_text, tags, visual_scenes."
    );
  }

  let lastError = null;
  for (const candidate of candidates) {
    try {
      const obj = parseJsonCandidate(candidate);
      return validateWorkflowObject(obj);
    } catch (err) {
      lastError = err;
    }
  }

  throw (
    lastError ||
    new Error(
      "A resposta não é um JSON válido. Peça ao ChatGPT: title, script_text, tags, visual_scenes."
    )
  );
};

/** @param {number} targetDurationSeconds */
globalThis.computeSceneCount = function computeSceneCount(targetDurationSeconds) {
  const secs = Math.max(20, Math.min(60, Number(targetDurationSeconds) || 30));
  return Math.max(7, Math.ceil(secs / 3));
};

function normalizeJsonInput(text) {
  return String(text)
    .replace(/\uFEFF/g, "")
    .replace(/[\u200B-\u200D\u2060]/g, "")
    .replace(/[\u201C\u201D\u201E\u00AB\u00BB\u2033\u2036]/g, '"')
    .replace(/[\u2018\u2019\u201A\u2032\u2035]/g, "'")
    .trim();
}

function collectJsonCandidates(raw) {
  const seen = new Set();
  const candidates = [];

  const add = (value) => {
    const candidate = String(value || "").trim();
    if (!candidate || seen.has(candidate)) return;
    seen.add(candidate);
    candidates.push(candidate);
  };

  const fences = raw.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi);
  for (const fence of fences) {
    add(fence[1]);
  }

  add(extractBalancedJsonObject(raw));

  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start !== -1 && end > start) {
    add(raw.slice(start, end + 1));
  }

  add(raw);
  return candidates;
}

function extractBalancedJsonObject(raw) {
  const start = raw.indexOf("{");
  if (start === -1) return "";

  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let i = start; i < raw.length; i += 1) {
    const char = raw[i];

    if (escaped) {
      escaped = false;
      continue;
    }

    if (inString) {
      if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }

    if (char === '"') {
      inString = true;
      continue;
    }

    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return raw.slice(start, i + 1);
    }
  }

  return "";
}

function parseJsonCandidate(candidate) {
  const attempts = [
    candidate,
    candidate.replace(/,\s*([}\]])/g, "$1"),
    candidate.replace(/'([^'\\]*(?:\\.[^'\\]*)*)'/g, '"$1"'),
  ];

  let lastError = null;
  for (const attempt of attempts) {
    try {
      return JSON.parse(attempt);
    } catch (err) {
      lastError = err;
    }
  }

  throw lastError || new Error("JSON inválido.");
}

function pickString(obj, ...keys) {
  for (const key of keys) {
    const val = obj[key];
    if (val != null && String(val).trim()) return String(val).trim();
  }
  return "";
}

function normalizeVisualScenes(raw, legacyPrompt) {
  const scenes = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (!item || typeof item !== "object") continue;
      const prompt_en = pickString(item, "prompt_en", "promptEn", "prompt", "prompt_imagem", "promptImagem");
      if (!prompt_en) continue;
      scenes.push({
        prompt_en,
        keywords_pt: pickString(item, "keywords_pt", "keywordsPt", "keywords"),
      });
    }
  }
  if (!scenes.length && legacyPrompt) {
    scenes.push({ prompt_en: legacyPrompt, keywords_pt: "" });
  }
  return scenes;
}

function normalizeTags(raw) {
  if (Array.isArray(raw)) {
    return raw.map((t) => String(t).trim()).filter(Boolean);
  }
  if (typeof raw === "string" && raw.trim()) {
    return raw.split(/[,;#]+/).map((t) => t.trim()).filter(Boolean);
  }
  return [];
}

function validateWorkflowObject(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
    throw new Error(
      "A resposta não é um JSON válido. Campos: title, script_text, tags, visual_scenes."
    );
  }

  const script_text = pickString(
    obj,
    "script_text",
    "scriptText",
    "roteiro_post",
    "roteiroPost"
  );
  const title = pickString(obj, "title", "titulo", "Titulo") || script_text.slice(0, 80);
  const youtube_body = pickString(obj, "youtube_body", "youtubeBody", "descricao", "description") || script_text;
  const topic_key = pickString(obj, "topic_key", "topicKey");
  const legacyPrompt = pickString(obj, "prompt_imagem", "promptImagem");
  const visual_scenes = normalizeVisualScenes(
    obj.visual_scenes ?? obj.visualScenes ?? obj.scenes,
    legacyPrompt
  );
  const tags = normalizeTags(obj.tags);

  if (!script_text) {
    throw new Error('JSON incompleto. Campo obrigatório: "script_text" (ou alias "roteiro_post").');
  }
  if (!visual_scenes.length) {
    throw new Error(
      'JSON incompleto. Campo obrigatório: "visual_scenes" com pelo menos uma cena (ou "prompt_imagem").'
    );
  }
  if (tags.length < 3) {
    throw new Error('JSON incompleto. Campo "tags" precisa de pelo menos 3 entradas.');
  }

  for (let i = 0; i < visual_scenes.length; i += 1) {
    if (!visual_scenes[i].prompt_en) {
      throw new Error(`visual_scenes[${i}].prompt_en está vazio.`);
    }
  }

  return {
    title,
    script_text,
    youtube_body,
    tags,
    visual_scenes,
    topic_key,
    roteiro_post: script_text,
    prompt_imagem: visual_scenes[0].prompt_en,
  };
}
