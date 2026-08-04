/**
 * Look-alike vertical bars must not break markdown tables.
 *
 * Reported live: "there seems to be two types of vertical bar … it renders
 * only one of them as a table". Models emit U+FF5C (fullwidth, from a CJK
 * tokenizer) and box-drawing bars; GFM only understands U+007C, so one stray
 * character turns a whole table back into a paragraph with pipes in it.
 *
 * The repair is scoped, and these tests are mostly about the SCOPE: a blanket
 * replace would corrupt prose that legitimately contains a fullwidth bar and
 * would promote `tree` output into a table. Only a run of lines that already
 * has a `---|---` separator is rewritten, and never inside a fence.
 *
 * Runs the SHIPPED function — the source slice is transpiled with the
 * project's own bundler rather than reimplemented here.
 *
 * Run: node web/test/markdown-bars.test.mjs
 */
// Behaviour check on the real exported function, compiled out of the .tsx.
import { readFileSync, writeFileSync } from "node:fs"
const src = readFileSync(new URL("../src/shared/lib/markdown.tsx", import.meta.url), "utf8")
const start = src.indexOf("const BAR_VARIANTS")
const end = src.indexOf("export function escapeBareOrdinals")
import { execFileSync } from "node:child_process"
writeFileSync("/tmp/_bars.ts", src.slice(start, end).replace(/export function/g, "function")
  + "\nexport { normalizeTableBars }\n")
// Transpile with the esbuild vite already depends on — no hand-rolled type
// stripping, so the code under test is the code that ships.
// rolldown is what vite bundles with here; use it rather than hand-rolling
// type stripping, so the code under test is the code that ships.
execFileSync("node_modules/.bin/rolldown",
  ["/tmp/_bars.ts", "-o", "/tmp/_bars.mjs", "--format", "esm"],
  { cwd: new URL("..", import.meta.url).pathname, stdio: "pipe" })
const { normalizeTableBars } = await import("/tmp/_bars.mjs")

let pass = 0, fail = 0
const t = (name, got, want) => {
  const ok = got === want
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}` + (ok ? "" : `\n     got:  ${JSON.stringify(got)}\n     want: ${JSON.stringify(want)}`))
  ok ? pass++ : fail++
}

const FW = "｜"   // ｜ fullwidth — what a CJK tokenizer emits
const BOX = "│"  // │ box drawing
const BROKEN = "¦" // ¦

t("fullwidth bars in a table become ASCII",
  normalizeTableBars(`${FW}a${FW}b${FW}\n${FW}---${FW}---${FW}\n${FW}1${FW}2${FW}`),
  "|a|b|\n|---|---|\n|1|2|")

t("a MIXED row (the common real case) is repaired",
  normalizeTableBars(`| a | b |\n|---|---|\n| 1 ${FW} 2 |`),
  "| a | b |\n|---|---|\n| 1 | 2 |")

t("box-drawing bars in a real table are repaired",
  normalizeTableBars(`${BOX}a${BOX}b${BOX}\n${BOX}---${BOX}---${BOX}\n${BOX}1${BOX}2${BOX}`),
  "|a|b|\n|---|---|\n|1|2|")

t("broken bar too", normalizeTableBars(`${BROKEN}a${BROKEN}b${BROKEN}\n|---|---|\n${BROKEN}1${BROKEN}2${BROKEN}`),
  "|a|b|\n|---|---|\n|1|2|")

// The guards: these must NOT be touched.
t("prose containing a fullwidth bar is left alone",
  normalizeTableBars(`The ${FW} character is fullwidth.`),
  `The ${FW} character is fullwidth.`)

t("tree output (bars, no separator row) is NOT promoted to a table",
  normalizeTableBars(`${BOX} src\n${BOX} ${BOX} main.ts\n${BOX} test`),
  `${BOX} src\n${BOX} ${BOX} main.ts\n${BOX} test`)

t("fenced code is untouched",
  normalizeTableBars("```\n" + `${FW}a${FW}b${FW}\n${FW}---${FW}---${FW}\n` + "```"),
  "```\n" + `${FW}a${FW}b${FW}\n${FW}---${FW}---${FW}\n` + "```")

t("a table AFTER a fence still gets fixed",
  normalizeTableBars("```\ncode\n```\n" + `${FW}a${FW}b${FW}\n|---|---|\n${FW}1${FW}2${FW}`),
  "```\ncode\n```\n" + "|a|b|\n|---|---|\n|1|2|")

t("plain ASCII table is returned unchanged (fast path)",
  normalizeTableBars("| a | b |\n|---|---|\n| 1 | 2 |"),
  "| a | b |\n|---|---|\n| 1 | 2 |")

t("two fullwidth bars with no separator stay prose",
  normalizeTableBars(`a ${FW} b ${FW} c\nd ${FW} e ${FW} f`),
  `a ${FW} b ${FW} c\nd ${FW} e ${FW} f`)

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
