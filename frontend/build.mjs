#!/usr/bin/env node
// esbuild entry: bundle the canonical frontend sources into dist/.
// The sources stay plain browser scripts (no module split yet); dist/ is the
// single artifact consumed by index.html through /app.js and /app.css.
import * as esbuild from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.join(here, "dist");

await esbuild.build({
  entryPoints: [path.join(here, "app.js"), path.join(here, "app.css")],
  outdir,
  bundle: false,
  minify: true,
  charset: "utf8",
  target: ["es2018"],
  logLevel: "info",
});
