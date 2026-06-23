/**
 * Fixture test: submit selector markers in labs_flow_sample.html.
 * Run: node scripts/test-flow-submit-selector.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const fixturePath = join(__dirname, "../docs/samples/labs_flow_sample.html");
const html = readFileSync(fixturePath, "utf8");

const hasSubmitClass = /button[^>]*class="[^"]*sc-26b30722-5[^"]*"[^>]*>[\s\S]*?arrow_forward/.test(
  html
);
const hasAddMenu =
  /button[^>]*aria-haspopup="dialog"[^>]*>[\s\S]*?add_2/.test(html) ||
  /button[^>]*>[\s\S]*?add_2[\s\S]*?aria-haspopup="dialog"/.test(html);

if (!hasSubmitClass) {
  console.error("Expected submit button: sc-26b30722-5 + arrow_forward");
  process.exit(1);
}
if (!hasAddMenu) {
  console.error("Expected add menu button: add_2 + aria-haspopup=dialog");
  process.exit(1);
}

const arrowIdx = html.indexOf("arrow_forward");
const addIdx = html.indexOf("add_2");
const submitSlice = html.slice(Math.max(0, arrowIdx - 300), arrowIdx + 50);
const addSlice = html.slice(Math.max(0, addIdx - 300), addIdx + 50);

if (submitSlice.includes("add_2")) {
  console.error("arrow_forward region must not include add_2");
  process.exit(1);
}
if (!addSlice.includes('aria-haspopup="dialog"')) {
  console.error("add_2 button must be a dialog trigger");
  process.exit(1);
}

console.log("OK — flow submit selector fixture test passed");
