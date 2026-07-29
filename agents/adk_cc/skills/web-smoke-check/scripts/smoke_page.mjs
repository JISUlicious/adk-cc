#!/usr/bin/env node
/**
 * Load a real page in a real DOM, drive it, and report what a user would see.
 *
 * Why this exists: a verifier with no DOM available hand-rolls one. Watched
 * live, it probes for chromium, google-chrome, playwright and jsdom, finds
 * none, and builds a `vm` sandbox with a ClassList stub — every run a fresh
 * improvisation, so the quality of verification swings with it. One improvised
 * shim missed a game whose vote result was erased in the same tick it was
 * written; another caught a startup crash. Same prompt, same model.
 *
 * Usage:
 *   node smoke_page.mjs <page.html> <check.mjs> [--json]
 *
 * `check.mjs` default-exports async ({ window, document, click, text, settle })
 * and throws to fail. Console errors and uncaught page exceptions are collected
 * whether or not the check looks for them.
 *
 * Exit codes: 0 pass, 1 check failed, 2 no DOM runtime, 3 usage/load error.
 *
 * The report always names the TIER that ran, because coverage differs and a
 * silent tier invites a claim the run cannot support:
 *   playwright — real browser: layout, canvas, CSS all real
 *   jsdom      — full DOM + events, NO layout and NO canvas rendering
 */

import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const args = process.argv.slice(2).filter((a) => a !== "--json");
const asJson = process.argv.includes("--json");
if (args.length < 2) {
  console.error("usage: node smoke_page.mjs <page.html> <check.mjs> [--json]");
  process.exit(3);
}
const pagePath = path.resolve(args[0]);
const checkPath = path.resolve(args[1]);

/** Resolve a module from the workspace, then from the shared adk-cc cache. */
function tryRequire(name) {
  const roots = [
    process.cwd(),
    process.env.ADK_CC_WEB_RUNTIME_DIR || "",
    path.join(process.env.HOME || "", ".adk-cc", "web-runtime"),
  ].filter(Boolean);
  for (const root of roots) {
    try {
      const req = createRequire(path.join(root, "noop.js"));
      return req(name);
    } catch {
      /* try the next root */
    }
  }
  return null;
}

const findings = [];
const consoleErrors = [];
const pageErrors = [];

async function runWithJsdom(JSDOM) {
  const html = readFileSync(pagePath, "utf8");
  const dom = new JSDOM(html, {
    url: pathToFileURL(pagePath).href,
    runScripts: "dangerously",     // the page's OWN scripts, unmodified
    resources: "usable",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.addEventListener("error", (e) =>
    pageErrors.push(String(e?.error?.stack || e?.message || e)));
  const origError = window.console.error;
  window.console.error = (...a) => {
    consoleErrors.push(a.map(String).join(" "));
    origError?.apply(window.console, a);
  };
  // External <script src> files load asynchronously; without this the check
  // races the page's own wiring and every selector comes back null.
  await new Promise((resolve) => {
    if (window.document.readyState === "complete") return resolve();
    window.addEventListener("load", resolve);
    setTimeout(resolve, 4000);
  });
  return window;
}

async function main() {
  let window = null;
  let tier = null;

  const JSDOM = tryRequire("jsdom")?.JSDOM;
  if (JSDOM) {
    tier = "jsdom";
    window = await runWithJsdom(JSDOM);
  }

  if (!window) {
    const msg =
      "no DOM runtime available. Install one into the shared cache:\n" +
      "  mkdir -p ~/.adk-cc/web-runtime && cd ~/.adk-cc/web-runtime && " +
      "npm i jsdom\n" +
      "Until then a behaviour claim about this page is NOT verified — say so " +
      "rather than substituting a syntax check or a re-implementation.";
    if (asJson) console.log(JSON.stringify({ ok: false, tier: null, error: msg }));
    else console.error(msg);
    process.exit(2);
  }

  const { document } = window;
  const helpers = {
    window,
    document,
    /** Click a selector and let handlers settle. Throws if it isn't there —
     *  "the button does nothing" and "the button does not exist" are different
     *  failures and a check should not conflate them. */
    async click(selector) {
      const el = document.querySelector(selector);
      if (!el) throw new Error(`click: no element matches ${selector}`);
      el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      await helpers.settle();
      return el;
    },
    text(selector) {
      const el = document.querySelector(selector);
      return el ? (el.textContent || "").trim() : null;
    },
    /** Flush microtasks + timers so state written and then cleared in the same
     *  tick is observed as the user would see it: cleared. */
    settle(ms = 0) {
      return new Promise((r) => setTimeout(r, ms));
    },
    findings,
  };

  const mod = await import(pathToFileURL(checkPath).href);
  const check = mod.default || mod.check;
  if (typeof check !== "function") {
    console.error(`${checkPath} must default-export a function`);
    process.exit(3);
  }

  let ok = true;
  let error = null;
  try {
    await check(helpers);
  } catch (e) {
    ok = false;
    error = String(e?.stack || e);
  }

  const report = {
    ok: ok && pageErrors.length === 0,
    tier,
    coverage:
      tier === "jsdom"
        ? "DOM + events. NO layout, NO canvas rendering — do not claim visual correctness."
        : "real browser",
    page: pagePath,
    consoleErrors,
    pageErrors,
    findings,
    error,
  };
  if (asJson) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`tier: ${tier}  (${report.coverage})`);
    if (findings.length) findings.forEach((f) => console.log(`  note: ${f}`));
    if (consoleErrors.length) console.log(`  console.error: ${consoleErrors.length}`);
    consoleErrors.slice(0, 5).forEach((c) => console.log(`    ${c.slice(0, 160)}`));
    if (pageErrors.length) console.log(`  uncaught: ${pageErrors.length}`);
    pageErrors.slice(0, 5).forEach((c) => console.log(`    ${c.slice(0, 160)}`));
    console.log(report.ok ? "RESULT: PASS" : "RESULT: FAIL");
    if (error) console.log(error.slice(0, 1200));
  }
  process.exit(report.ok ? 0 : 1);
}

main().catch((e) => {
  console.error(String(e?.stack || e));
  process.exit(3);
});
