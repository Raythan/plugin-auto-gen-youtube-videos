/**
 * Fixture test: labs_flow_sample.html should yield 4 generated images, 0 avatars.
 * Run: node scripts/test-flow-capture.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const fixturePath = join(__dirname, "../docs/samples/labs_flow_sample.html");
const html = readFileSync(fixturePath, "utf8");

function isFlowGeneratedImageSrc(src) {
  return src.includes("media.getMediaUrlRedirect");
}

function isExcludedFlowImage({ src, alt, inHeader }) {
  const s = src.toLowerCase();
  const a = alt.toLowerCase();
  if (inHeader) return true;
  if (/\/fx\/icons\//i.test(s)) return true;
  if (/perfil|profile|avatar|user photo/i.test(a)) return true;
  if (s.includes("googleusercontent")) return true;
  return false;
}

function isFlowGeneratedImage({ src, alt, inHeader }) {
  if (isExcludedFlowImage({ src, alt, inHeader })) return false;
  if (isFlowGeneratedImageSrc(src)) return true;
  if (/imagem gerada|generated image/i.test(alt)) return true;
  return false;
}

const imgTags = [...html.matchAll(/<img\b[^>]*>/gi)];
const parsed = imgTags.map((match) => {
  const tag = match[0];
  const src = (tag.match(/\bsrc="([^"]*)"/i) || [])[1] || "";
  const alt = (tag.match(/\balt="([^"]*)"/i) || [])[1] || "";
  const inHeader = /flow-desktop-header/.test(
    html.slice(Math.max(0, match.index - 4000), match.index)
  );
  return { src, alt, inHeader };
});

const generated = parsed.filter((img) => isFlowGeneratedImage(img));
const avatars = parsed.filter(
  (img) => img.src.includes("googleusercontent") && !isFlowGeneratedImage(img)
);

console.log(`Total <img>: ${parsed.length}`);
console.log(`Generated (fixture): ${generated.length}`);
console.log(`Avatars excluded: ${avatars.length}`);

if (generated.length !== 4) {
  console.error(`Expected 4 generated images, got ${generated.length}`);
  process.exit(1);
}
if (avatars.length !== 1) {
  console.error(`Expected 1 avatar to exclude, got ${avatars.length}`);
  process.exit(1);
}
if (generated.some((g) => g.src.includes("googleusercontent"))) {
  console.error("Generated set must not include googleusercontent avatar");
  process.exit(1);
}

console.log("OK — flow capture fixture test passed");
