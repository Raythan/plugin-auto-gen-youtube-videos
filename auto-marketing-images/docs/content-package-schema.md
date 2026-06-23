# Content package schema (plugin → console)

Pacotes de conteúdo gerados pelo plugin `plugin-auto-gen` e consumidos pelo console
`auto-marketing-images` para renderização de vídeo.

## Estrutura no disco

Caminho configurável via `content_bridge.inbox_dir` (default: `data/pending_content/`).

```
{inbox_dir}/{id}/
  manifest.json
  images/
    01.png
    02.png
    ...
```

- `{id}`: identificador único, formato `YYYYMMDDTHHMMSS_{uuid8}`.
- Imagens: PNG, 1080×1920 recomendado, nomeadas `01.png` … `NN.png` em ordem das cenas.

## manifest.json

```json
{
  "id": "20260619T120000_a1b2c3d4",
  "created_at": "2026-06-19T15:00:00+00:00",
  "channel_id": "rpjtechgroup",
  "source": "plugin-auto-gen",
  "script": {
    "title": "ChatGPT no atendimento",
    "script_text": "Roteiro falado em PT-BR para TTS...",
    "youtube_body": "Descrição completa para YouTube com hashtags...",
    "tags": ["IA", "ChatGPT", "negocios"],
    "visual_scenes": [
      { "prompt_en": "cinematic vertical 9:16 office...", "keywords_pt": "ia, atendimento" }
    ],
    "topic_key": "chatgpt-atendimento"
  }
}
```

### Campos obrigatórios (nível raiz)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | ID único do pacote |
| `created_at` | string ISO-8601 | Momento de criação |
| `channel_id` | string | Canal alvo (`config/channels_config_structure/*.yaml`) |
| `source` | string | Sempre `plugin-auto-gen` |
| `script` | object | Payload compatível com `ScriptResult` |

### Campos obrigatórios (`script`)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `title` | string | Título do vídeo |
| `script_text` | string | Texto falado (TTS), PT-BR |
| `tags` | string[] | Mínimo 3 tags |
| `visual_scenes` | array | Cenas visuais; ver abaixo |
| `youtube_body` | string | Opcional; descrição YouTube |
| `topic_key` | string | Slug kebab-case para anti-repetição |

### `visual_scenes[]`

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `prompt_en` | string | Prompt em inglês usado para gerar a imagem |
| `keywords_pt` | string | Opcional; palavras-chave PT |

Regra de quantidade: `ceil(max_seconds / 3)` cenas (default 20–35s → 7–12 cenas).

## JSON do ChatGPT (plugin)

O plugin aceita o schema completo acima ou aliases legados:

| Legado | Mapeia para |
|--------|-------------|
| `roteiro_post` / `roteiroPost` | `script_text` |
| `prompt_imagem` / `promptImagem` | primeira cena `visual_scenes[0].prompt_en` |

## HTTP bridge

`POST /content` — `multipart/form-data`:

- `manifest`: JSON string (campos acima, sem `id`/`created_at` — o bridge gera)
- `image_01` … `image_N`: arquivos PNG

Respostas:

- `201`: `{ "id": "...", "path": "..." }`
- `400`: payload inválido
- `409`: pacote com mesmo `id` já existe

`GET /health` — `{ "status": "ok" }`

`GET /content/stats` — `{ "pending": N }`

## Exemplo válido

Ver `docs/examples/content-package-valid/` (manifest + 3 imagens de teste geradas pelo bridge).

## Exemplo inválido

```json
{
  "channel_id": "rpjtechgroup",
  "script": {
    "title": "Sem roteiro",
    "tags": ["a"],
    "visual_scenes": []
  }
}
```

Falhas esperadas: `script_text` vazio, menos de 3 tags, `visual_scenes` vazio.
