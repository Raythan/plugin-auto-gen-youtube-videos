# Google Flow DOM notes

Mapeamento para automação do plugin (baseado em `docs/samples/labs_flow_sample.html`).

## URLs

| Tipo | Padrão |
|------|--------|
| Entrada (redirect) | `https://labs.google/fx/tools/image-fx` |
| Projeto | `https://labs.google/fx/{locale}/tools/flow/project/{uuid}?from=imagefx` |
| Mídia gerada | `/fx/api/trpc/media.getMediaUrlRedirect?name={uuid}` |

O plugin salva a URL do projeto em `flowProjectUrl` após o primeiro redirect.

## Elementos

| Ação | Seletor / texto |
|------|-----------------|
| Login / bloqueio | `Sign in`, `Entrar`, `isn't available in your country` |
| Prompt | `[role="textbox"][data-slate-editor="true"][contenteditable="true"]` — placeholder `O que você quer criar?` |
| **Enviar (correto)** | Ícone `arrow_forward`, classe `sc-26b30722-5`, container `sc-26b30722-10`, sr-only `Criar` |
| **Menu + (errado para enviar)** | Ícone `add_2`, `aria-haspopup="dialog"`, sr-only `Criar` — abre menu |
| Modo imagem | Dentro do dialog do `add_2`: `Criar imagem` / `Create image` |
| Proporção 9:16 | Botão ícone `crop_9_16` (Nano Banana) — não clicar no menu inteiro |
| Imagem gerada | `img[alt="Imagem gerada"]`, `src` com `media.getMediaUrlRedirect` |
| Tile | `[data-tile-id]` |
| **Excluir** | `#flow-desktop-header img`, alt `Imagem do perfil do usuário` |

## Dois botões "Criar" no composer

| Botão | Ícone | Container | Atributos |
|-------|-------|-----------|-----------|
| Menu + | `add_2` | `sc-26b30722-2` | `aria-haspopup="dialog"` |
| Enviar | `arrow_forward` | `sc-26b30722-10` | sem `aria-haspopup`, classe `sc-26b30722-5` |

**Nunca** use `textContent.includes('Criar')` para encontrar o botão de envio.

Snippet correto no console:

```js
document.querySelector("button[class*='sc-26b30722-5'] i.google-symbols")?.closest("button")
```

Anti-padrão (clica no +):

```js
Array.from(document.querySelectorAll("button"))
  .find((btn) => btn.textContent.includes("Criar") && btn.querySelector("i")?.textContent.includes("add_2"))
```

## Automação no plugin

- Preenchimento: content script isolado (`labs-flow.js`)
- Clique enviar: script MAIN (`labs-flow-page.js`) via evento `plugin-auto-gen-flow-submit`
- Seletores compartilhados: [`shared/flow-composer-dom.js`](../shared/flow-composer-dom.js)
- Hover (~180ms) antes do clique no botão `arrow_forward`

## Falso positivo (avatar)

O avatar do header (`googleusercontent`, 96×96) passava no filtro antigo. A captura agora:

- Aceita `media.getMediaUrlRedirect` e `alt="Imagem gerada"`
- Rejeita imagens no header e avatares `googleusercontent` ≤128px

Ver [`shared/image-capture.js`](../shared/image-capture.js) e [`content/labs-flow.js`](../content/labs-flow.js).
