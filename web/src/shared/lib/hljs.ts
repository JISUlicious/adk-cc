/**
 * A highlight.js instance with only a CURATED language subset registered, so we
 * pull in a handful of grammars instead of the full ~190-language bundle. Token
 * colors are theme-aware CSS vars in index.css (`.hljs-*`), not a shipped theme.
 *
 * Add a language here (and an extension in `filetypes.langFromPath`) when a file
 * type shows up in practice. Each language's own aliases are auto-registered
 * (e.g. registering `typescript` also gives `ts`), plus the extra aliases below.
 */
import hljs from "highlight.js/lib/core"
import bash from "highlight.js/lib/languages/bash"
import c from "highlight.js/lib/languages/c"
import cpp from "highlight.js/lib/languages/cpp"
import css from "highlight.js/lib/languages/css"
import diff from "highlight.js/lib/languages/diff"
import dockerfile from "highlight.js/lib/languages/dockerfile"
import go from "highlight.js/lib/languages/go"
import ini from "highlight.js/lib/languages/ini"
import java from "highlight.js/lib/languages/java"
import javascript from "highlight.js/lib/languages/javascript"
import json from "highlight.js/lib/languages/json"
import markdown from "highlight.js/lib/languages/markdown"
import python from "highlight.js/lib/languages/python"
import ruby from "highlight.js/lib/languages/ruby"
import rust from "highlight.js/lib/languages/rust"
import sql from "highlight.js/lib/languages/sql"
import typescript from "highlight.js/lib/languages/typescript"
import xml from "highlight.js/lib/languages/xml"
import yaml from "highlight.js/lib/languages/yaml"

const LANGUAGES = {
  bash, c, cpp, css, diff, dockerfile, go, ini, java, javascript, json,
  markdown, python, ruby, rust, sql, typescript, xml, yaml,
}

for (const [name, def] of Object.entries(LANGUAGES)) {
  hljs.registerLanguage(name, def)
}

// Extra aliases not always declared by the grammar module.
hljs.registerAliases(["tsx"], { languageName: "typescript" })
hljs.registerAliases(["jsx"], { languageName: "javascript" })
hljs.registerAliases(["toml"], { languageName: "ini" })

hljs.configure({ classPrefix: "hljs-", ignoreUnescapedHTML: true })

export default hljs
