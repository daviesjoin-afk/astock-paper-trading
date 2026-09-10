#!/usr/bin/env node
// esbuild entry: bundle the canonical frontend sources into dist/.
//
// PR-55：源已按 feature ownership 拆到 src/**（ESM）与 styles/**（CSS 片段），
// 产物路径不变（dist/app.js、dist/app.css），index.html 仍只加载这两个文件。
//
// - JS：src/app.js 是入口（build id + 兼容桥 + boot），打包成 **IIFE**，
//   这样 `<script src="/app.js" defer>` 这种经典脚本加载方式不用改；
//   模块化后不再自动全局的函数由 src/bridge.js 逐个挂到 window（inline onclick 依赖）。
// - CSS：styles/index.css 用 @import 按原顺序引用各片段，bundle 会内联成单文件，
//   层叠顺序与拆分前的 app.css 等价。
import * as esbuild from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.join(here, "dist");

const common = { bundle: true, minify: true, charset: "utf8", logLevel: "info" };

await esbuild.build({
  ...common,
  entryPoints: [path.join(here, "src", "app.js")],
  outfile: path.join(outdir, "app.js"),
  format: "iife",
  target: ["es2018"],
});

await esbuild.build({
  ...common,
  entryPoints: [path.join(here, "styles", "index.css")],
  outfile: path.join(outdir, "app.css"),
});
