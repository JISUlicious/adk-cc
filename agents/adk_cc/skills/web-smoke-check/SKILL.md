---
name: web-smoke-check
description: >
  Verify a page by driving it in a real DOM: click what a user clicks, assert
  what they would see. Use for any page, game, or browser app — reading the
  code or re-implementing its logic proves nothing.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the page was loaded and driven by the runner (its reported tier is quoted), not read or re-implemented", "the check asserts state AFTER the action settles, not merely that a handler exists", "console errors and uncaught page exceptions from the run are reported, including when zero", "coverage limits of the tier are stated before any claim about appearance"]}
---

# Web smoke check

Verification of a page has to load the page. This runs the page's **own
unmodified scripts** in a DOM and drives them.

## Run

```bash
node scripts/smoke_page.mjs <page.html> <check.mjs> [--json]
```

Exit codes: `0` pass, `1` the check failed, `2` no DOM runtime installed,
`3` usage error. The report always names the tier that ran.

## Write the check

`check.mjs` default-exports an async function and throws to fail:

```js
export default async ({ click, text, document, settle }) => {
  await click("#start-game");
  await click("#vote-option-1");
  await click("#resolve-vote");
  const shown = text("#vote-result");
  if (!shown) throw new Error("vote produced no visible result");
};
```

Helpers: `click(selector)` (throws if absent — "does nothing" and "does not
exist" are different bugs), `text(selector)`, `settle(ms)`, plus raw `window`
and `document`.

## Assert what settles, not what fires

The bug this exists for: a game wrote the vote outcome to the DOM and then
began the next round in the same tick, which cleared it. Every element existed,
every handler fired, and no player ever saw who was voted out. A check that
asserts "the handler ran" passes; one that reads the text afterwards fails.

## If there is no runtime

Exit code 2 means nothing is installed. Provision the shared cache once:

```bash
mkdir -p ~/.adk-cc/web-runtime && cd ~/.adk-cc/web-runtime && npm i jsdom
```

Until then, say the behaviour is unverified. Do not substitute a syntax check
or a re-implementation and report it as verification.

## Coverage

`jsdom` gives DOM, events and script execution — **no layout, no canvas
rendering**. It cannot tell you something is visually correct, positioned, or
painted. Claims about appearance need a real browser; say which you used.
