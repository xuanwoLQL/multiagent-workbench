/**
 * 前端纯函数离线回放器（Node，无浏览器）。
 *
 * 为什么需要：`multiagent/web/app.js` 是零构建的原生 JS，pytest 无法 import；
 * 但实时流的「块合并」逻辑（`buildLiveBlocks` / `applyToolEvent` / `normalizeChunkedText`）
 * 是**纯函数**（源码注释也写了「便于离线回放验证」）。这里按函数名从 app.js 里抽出
 * 源码求值，用真实抓包 `.e2e/live-capture.json` 回放，断言「1612 条 raw → 19 个块」。
 *
 * 用法：
 *   node tests/live_blocks_harness.mjs blocks .e2e/live-capture.json
 *   node tests/live_blocks_harness.mjs normalize .e2e/live-board-sample.json
 *   node tests/live_blocks_harness.mjs cases
 *
 * 输出：单行 JSON（stdout），失败时非 0 退出。
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const APP_JS = resolve("multiagent/web/app.js");

/** 按函数名从 app.js 抽出完整源码（大括号配平，能正确处理字符串里的花括号）。 */
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`app.js 里找不到 function ${name}(`);
  const open = source.indexOf("{", start);
  let depth = 0;
  let inString = null;
  let inTemplate = false;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    const prev = source[i - 1];
    if (inString) {
      if (ch === inString && prev !== "\\") inString = null;
      continue;
    }
    if (inTemplate) {
      if (ch === "`" && prev !== "\\") inTemplate = false;
      continue;
    }
    if (ch === '"' || ch === "'") { inString = ch; continue; }
    if (ch === "`") { inTemplate = true; continue; }
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`function ${name} 的大括号不配平`);
}

function loadPureFunctions() {
  const app = readFileSync(APP_JS, "utf8");
  const names = ["firstLine", "applyToolEvent", "buildLiveBlocks", "normalizeChunkedText"];
  const src = names.map((name) => extractFunction(app, name)).join("\n");
  const factory = new Function(`${src}; return { ${names.join(", ")} };`);
  return factory();
}

function readJson(path) {
  return JSON.parse(readFileSync(resolve(path), "utf8"));
}

function blocksReport(path) {
  const { buildLiveBlocks } = loadPureFunctions();
  const events = readJson(path);
  const blocks = buildLiveBlocks(events);
  const byType = {};
  for (const block of blocks) byType[block.type] = (byType[block.type] || 0) + 1;

  const thoughtBlocks = blocks.filter((b) => b.type === "thought");
  const messageBlocks = blocks.filter((b) => b.type === "message");
  const toolBlocks = blocks.filter((b) => b.type === "tool");
  const usageBlocks = blocks.filter((b) => b.type === "usage");
  const allText = blocks.map((b) => b.content || b.summary || "").join("\n");

  const singleTokenBlocks = thoughtBlocks.concat(messageBlocks).filter(
    (b) => b.content.replace(/\s/g, "").length <= 3 && b.chunk_count <= 1,
  ).length;

  return {
    mode: "blocks",
    file: path,
    events: events.length,
    blocks: blocks.length,
    byType,
    thoughtChunks: thoughtBlocks.reduce((n, b) => n + b.chunk_count, 0),
    messageChunks: messageBlocks.reduce((n, b) => n + b.chunk_count, 0),
    toolChunks: toolBlocks.reduce((n, b) => n + b.chunk_count, 0),
    thoughtBlocks: thoughtBlocks.map((b) => ({
      id: b.id,
      chunkCount: b.chunk_count,
      contentLen: b.content.length,
      head: b.content.slice(0, 40),
    })),
    messageBlocks: messageBlocks.map((b) => ({
      id: b.id,
      chunkCount: b.chunk_count,
      contentLen: b.content.length,
      head: b.content.slice(0, 40),
    })),
    toolBlocks: toolBlocks.map((b) => ({
      id: b.id,
      toolId: b.tool_id,
      title: b.title,
      command: b.command,
      status: b.status,
      hasCommand: Boolean(b.command),
      hasInput: b.input !== null && b.input !== undefined,
      hasOutput: b.output !== null && b.output !== undefined,
      locations: Array.isArray(b.locations) ? b.locations.length : 0,
      chunkCount: b.chunk_count,
    })),
    usageCount: usageBlocks.length,
    maxBlockChars: Math.max(0, ...blocks.map((b) => (b.content || "").length)),
    singleTokenBlocks,
    hasMojibake: allText.includes("\uFFFD") || allText.includes("???"),
  };
}

function normalizeReport(path) {
  const { normalizeChunkedText } = loadPureFunctions();
  const sample = readJson(path);
  const rows = [];
  const push = (id, field, value) => {
    const before = String(value ?? "");
    if (!before) return;
    const after = normalizeChunkedText(before);
    rows.push({
      id,
      field,
      beforeLen: before.length,
      beforeLines: before.split(/\r?\n/).length,
      afterLen: after.length,
      afterLines: after.split(/\r?\n/).length,
      afterHead: after.slice(0, 120),
    });
  };
  for (const entry of sample.board || []) push(entry.id, "board.body", entry.body);
  for (const handoff of sample.handoffs || []) {
    push(handoff.id, "handoff.did", handoff.did);
    push(handoff.id, "handoff.notes", handoff.notes);
    for (const [index, item] of (handoff.open_issues || []).entries()) {
      push(handoff.id, `handoff.open_issues[${index}]`, item);
    }
    for (const [index, item] of (handoff.next_steps || []).entries()) {
      push(handoff.id, `handoff.next_steps[${index}]`, item);
    }
  }
  return { mode: "normalize", file: path, rows };
}

function casesReport() {
  const { normalizeChunkedText } = loadPureFunctions();
  const tokenLines = Array.from({ length: 40 }, (_, i) => (i % 2 ? "实现" : "检查")).join("\n");
  const paragraph = "这是一段正常的多行文本。\n第二行也是完整句子。\n第三行同样完整。";
  const code = "```js\nconst a = 1;\nconst b = 2;\n```";
  const shortText = "只有一行";
  const cjkTokens = ["我", "先", "检查", "工作", "区", "。", "", "然后", "开始", "实现"].join("\n");
  return {
    mode: "cases",
    cases: {
      shortText: normalizeChunkedText(shortText),
      paragraphUnchanged: normalizeChunkedText(paragraph) === paragraph,
      codeBlockUnchanged: normalizeChunkedText(code) === code,
      empty: normalizeChunkedText(""),
      tokenLinesBefore: tokenLines.split("\n").length,
      tokenLinesAfter: normalizeChunkedText(tokenLines).split("\n").length,
      tokenLinesResult: normalizeChunkedText(tokenLines).slice(0, 80),
      cjkBefore: cjkTokens.split("\n").length,
      cjkAfter: normalizeChunkedText(cjkTokens).split("\n").length,
      cjkResult: normalizeChunkedText(cjkTokens),
    },
  };
}

function main() {
  const [mode, file] = process.argv.slice(2);
  let report;
  if (mode === "blocks") report = blocksReport(file);
  else if (mode === "normalize") report = normalizeReport(file);
  else if (mode === "cases") report = casesReport();
  else {
    process.stderr.write("usage: node tests/live_blocks_harness.mjs blocks|normalize|cases [json]\n");
    process.exit(2);
  }
  process.stdout.write(JSON.stringify(report));
}

main();
