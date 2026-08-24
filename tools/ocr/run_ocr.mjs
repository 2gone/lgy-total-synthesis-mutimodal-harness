import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { createWorker } from "tesseract.js";

const require = createRequire(import.meta.url);
const { version } = require("tesseract.js/package.json");
const toolDirectory = path.dirname(fileURLToPath(import.meta.url));

const [, , paperId, outputPath, ...pagePaths] = process.argv;
if (!paperId || !outputPath || pagePaths.length === 0) {
  throw new Error("Usage: node run_ocr.mjs PAPER_ID OUTPUT_JSON PAGE_PNG...");
}

function parseTsv(tsv) {
  const lines = String(tsv || "").split(/\r?\n/);
  const items = [];
  for (let index = 1; index < lines.length; index += 1) {
    const columns = lines[index].split("\t");
    if (columns.length < 12 || columns[0] !== "5") continue;
    const text = columns.slice(11).join("\t").trim();
    if (!text) continue;
    const left = Number(columns[6]);
    const top = Number(columns[7]);
    const width = Number(columns[8]);
    const height = Number(columns[9]);
    const confidence = Number(columns[10]);
    items.push({
      text,
      bbox_px: [left, top, left + width, top + height],
      confidence: Number.isFinite(confidence) && confidence >= 0 ? confidence : null,
    });
  }
  return items;
}

const worker = await createWorker("eng", 1, {
  langPath: toolDirectory,
  gzip: false,
  logger: (message) => {
    if (message.status === "recognizing text") {
      const percentage = Math.round((message.progress || 0) * 100);
      process.stdout.write(`progress=${percentage}%\n`);
    }
  },
});

const pages = [];
try {
  for (let index = 0; index < pagePaths.length; index += 1) {
    const result = await worker.recognize(pagePaths[index], {}, { text: true, tsv: true });
    pages.push({ page: index + 1, items: parseTsv(result.data.tsv) });
    process.stdout.write(`page=${index + 1}/${pagePaths.length} items=${pages.at(-1).items.length}\n`);
  }
} finally {
  await worker.terminate();
}

const payload = {
  paper_id: paperId,
  engine: {
    name: "Tesseract.js",
    version,
    language: "eng",
    output_granularity: "word",
  },
  pages,
};

fs.mkdirSync(path.dirname(outputPath), { recursive: true });
const temporaryPath = `${outputPath}.tmp-${process.pid}`;
fs.writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
fs.renameSync(temporaryPath, outputPath);
