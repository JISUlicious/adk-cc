"""Apple FM probe suite for adk-cc integration: availability, latency,
throughput, context ceiling, tool calling, structured output, concurrency,
and adk-cc task-fit (title / memory-capture / synthesis prompts)."""
import asyncio, time, json, sys
import apple_fm_sdk as fm

R = {}

def est_tokens(s): return len(s) // 4  # rough

async def main():
    model = fm.SystemLanguageModel()
    ok = model.is_available()
    print(f"[avail] {ok}")
    if not (ok if isinstance(ok, bool) else ok[0]):
        sys.exit(1)

    # --- 1. latency: short calls (cold then warm) ---
    for label in ("cold", "warm"):
        s = fm.LanguageModelSession()
        t0 = time.time()
        out = await s.respond("Reply with exactly: pong")
        dt = time.time() - t0
        print(f"[latency-{label}] {dt:.2f}s reply={out[:40]!r}")

    # --- 2. throughput: ~200-token generation ---
    s = fm.LanguageModelSession()
    t0 = time.time()
    out = await s.respond("Write a 150-word paragraph explaining what a hash map is.")
    dt = time.time() - t0
    toks = est_tokens(out)
    print(f"[throughput] {dt:.2f}s for ~{toks} tok -> ~{toks/dt:.1f} tok/s (len {len(out)})")

    # --- 3. context ceiling: grow prompt until context error ---
    filler = ("The quick brown fox jumps over the lazy dog near the river bank. " * 4).strip()
    lo, hi, ceiling = 500, 8000, None   # approx tokens
    probe_results = []
    for approx in (1000, 2000, 3000, 3500, 4000, 4500, 5000):
        text = (filler + " ") * (approx * 4 // len(filler))
        prompt = f"Summarize in ONE short sentence:\n{text}"
        s = fm.LanguageModelSession()
        try:
            t0 = time.time()
            await s.respond(prompt)
            probe_results.append((approx, "ok", round(time.time()-t0, 1)))
        except fm.ExceededContextWindowSizeError:
            probe_results.append((approx, "CONTEXT_EXCEEDED", 0))
        except Exception as e:
            probe_results.append((approx, type(e).__name__, 0))
    print(f"[context] {probe_results}")

    # --- 4. tool calling ---
    @fm.generable("Lookup parameters")
    class LookupParams:
        key: str = fm.guide("The config key to look up")

    calls = []
    class ConfigTool(fm.Tool):
        name = "get_config"
        description = "Returns the value of a project configuration key"
        @property
        def arguments_schema(self):
            return LookupParams.generation_schema()
        async def call(self, args):
            key = args.value(str, for_property="key")
            calls.append(key)
            return json.dumps({"key": key, "value": "postgres-16" if "db" in key.lower() else "unknown"})

    s = fm.LanguageModelSession(
        instructions="Use the get_config tool to answer configuration questions.",
        tools=[ConfigTool()])
    t0 = time.time()
    try:
        out = await s.respond("Which database engine does this project use? Check the db_engine config key.")
        print(f"[tools] {time.time()-t0:.2f}s tool_calls={calls} reply={out[:120]!r}")
    except Exception as e:
        print(f"[tools] FAILED {type(e).__name__}: {str(e)[:150]} (calls={calls})")

    # --- 5. structured output ---
    @fm.generable("A session title")
    class Title:
        title: str = fm.guide("3-6 word title for the conversation")

    s = fm.LanguageModelSession()
    try:
        out = await s.respond(
            "Conversation: user asked to fix a bug where portals in a 2D game never trigger; assistant patched collision code.",
            generating=Title)
        t = out.value(str, for_property="title") if hasattr(out, "value") else out
        print(f"[structured] title={t!r}")
    except Exception as e:
        print(f"[structured] FAILED {type(e).__name__}: {str(e)[:120]}")

    # --- 6. concurrency: two parallel sessions ---
    async def one(i):
        s = fm.LanguageModelSession()
        t0 = time.time()
        try:
            await s.respond(f"Reply with the number {i} only.")
            return f"ok {time.time()-t0:.1f}s"
        except Exception as e:
            return type(e).__name__
    r = await asyncio.gather(one(1), one(2))
    print(f"[concurrency] {r}")

    # --- 7. adk-cc task fits ---
    # 7a. session title (SessionTitlePlugin-style)
    s = fm.LanguageModelSession()
    out = await s.respond(
        "Give a 3-6 word title for this coding session, output ONLY the title:\n"
        "User: the market in my game won't open when I stand on the stall\n"
        "Assistant: Fixed the market anchor to use the stall bounding box; verified in browser.")
    print(f"[fit-title] {out.strip()[:80]!r}")

    # 7b. memory capture (adk-cc _CAPTURE_PROMPT, abbreviated)
    cap = ("You maintain long-term memory for an AI assistant. Record ONLY durable facts about the USER and THEIR work. "
           "Do NOT record general knowledge or one-off task steps.\n"
           "Output one fact per line, EXACTLY:\nTOPIC: <2-5 word topic> | <one concise sentence>\n"
           "If nothing is worth remembering, output: NONE\n\nTURN:\n"
           "User: deploy the game to my usual host\n"
           "Assistant: Deployed pixel-rogue to Fly.io as agreed; the project standardizes on Fly.io for hosting. "
           "Also confirmed the team uses Postgres 16 for the leaderboard service.")
    s = fm.LanguageModelSession()
    out = await s.respond(cap)
    print(f"[fit-capture] {out.strip()[:200]!r}")

    # 7c. synthesis (memory synth prompt)
    s = fm.LanguageModelSession()
    out = await s.respond(
        "Merge these statements about one topic into ONE concise, current fact (1-2 sentences). "
        "Prefer the newest when they conflict; keep specifics. Output only the merged fact.\n\n"
        "Existing: The project deploys to Render.\nNew (newest first):\n- Deployed to Fly.io today; Fly.io is now the standard target.\n- The project deploys to Render.")
    print(f"[fit-synth] {out.strip()[:160]!r}")

asyncio.run(main())
