# plugin-auto-gen — Content Bridge

Extensão de navegador (Firefox/Chrome, Manifest V3) que automatiza:

1. **ChatGPT** — gera JSON com roteiro (`script_text`), metadados e `visual_scenes` (N cenas)
2. **Google Flow** — gera uma imagem por cena (ImageFX redireciona para um projeto Flow)
3. **Bridge HTTP** — envia o pacote para o console Python (`auto-marketing-images`)

## Pré-requisitos

- Conta **ChatGPT** ativa no navegador
- Conta **Google** logada no [Google Flow](https://labs.google/fx/tools/flow) (créditos Flow conforme seu plano)
- Console Python com bridge rodando (ver abaixo)

## Instalação

1. Firefox: `about:debugging` → “Carregar extensão temporária” → pasta `plugin-auto-gen/`
2. Chrome: `chrome://extensions` → Modo desenvolvedor → “Carregar sem compactação”

## Configuração

No popup:

| Campo | Exemplo |
|-------|---------|
| URL do projeto Flow | `https://labs.google/fx/pt/tools/flow/project/cbbe68c0-...` (opcional) |
| URL do bridge | `http://127.0.0.1:8765/content` |
| ID do canal | `rpjtechgroup` |
| Duração alvo | `30` (define quantidade de cenas: `ceil(segundos/3)`) |

**URL do projeto Flow:** opcional. Se vazio, o plugin abre ImageFX e segue o redirect automático para o projeto; a URL é salva para os próximos ciclos. Cole a URL do projeto no navegador para fixar um projeto específico.

O console Python deve estar rodando:

```bash
cd auto-marketing-images
python -m src.pipeline console
```

## Uso

- **Play** — um ciclo: JSON (ChatGPT) → N imagens (Google Flow) → export
- **Loop** — repete a cada 2 horas
- **Parar** — cancela execução ou loop e fecha abas ChatGPT e Flow

## Prompt

Peça ao ChatGPT um JSON com: `title`, `script_text`, `youtube_body`, `tags`, `visual_scenes[]`, `topic_key`.

Cada `visual_scenes[].prompt_en` é enviado ao Flow em inglês (proporção 9:16 quando o seletor `crop_9_16` estiver disponível).

## Contrato de dados

Ver [auto-marketing-images/docs/content-package-schema.md](../auto-marketing-images/docs/content-package-schema.md).

## Manutenção

A UI do Google Flow muda com frequência. Seletores documentados em [docs/flow-dom-notes.md](docs/flow-dom-notes.md). Fixture de referência: [docs/samples/labs_flow_sample.html](docs/samples/labs_flow_sample.html).
