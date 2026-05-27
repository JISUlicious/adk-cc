import { useState } from "react"
import { HelpCircle } from "lucide-react"
import { Button } from "./ui/button"
import { cn } from "@/lib/utils"

/**
 * Renders an `ask_user_question` long-running tool call as an inline
 * structured form. Schema is defined by
 * `adk_cc/tools/schemas.py::AskUserQuestionArgs`:
 *
 *   questions[]: {
 *     question, header, multi_select,
 *     options[]: { label, description }
 *   }
 *
 * Each question gets buttons (single-select) or checkboxes (multi).
 * The "Other" free-form fallback is offered automatically — the tool
 * docstring promises it and the agent prompt-engineers assume it.
 *
 * On submit, the response shape is `{ <question_text>: <label> }` for
 * single-select and `{ <question_text>: [label, label, ...] }` for
 * multi-select, matching what the agent prompt expects.
 */

export interface AskOptionDef {
  label: string
  description: string
}

export interface AskQuestionDef {
  question: string
  header: string
  multi_select: boolean
  options: AskOptionDef[]
}

export interface AskUserQuestionArgsDef {
  questions: AskQuestionDef[]
}

type Answers = Record<string, string | string[]>

/** Sentinel for the auto-provided "Other" option. */
const OTHER_LABEL = "Other"

export function AskUserQuestionCard({
  args,
  onSubmit,
  disabled,
}: {
  args: AskUserQuestionArgsDef
  onSubmit: (response: Answers) => void
  disabled: boolean
}) {
  const [answers, setAnswers] = useState<Answers>({})
  const [otherText, setOtherText] = useState<Record<string, string>>({})

  function pickSingle(q: AskQuestionDef, label: string) {
    setAnswers((prev) => ({ ...prev, [q.question]: label }))
  }
  function toggleMulti(q: AskQuestionDef, label: string) {
    setAnswers((prev) => {
      const cur = Array.isArray(prev[q.question])
        ? (prev[q.question] as string[])
        : []
      const next = cur.includes(label)
        ? cur.filter((x) => x !== label)
        : [...cur, label]
      return { ...prev, [q.question]: next }
    })
  }
  function isPicked(q: AskQuestionDef, label: string): boolean {
    const v = answers[q.question]
    if (q.multi_select) {
      return Array.isArray(v) && v.includes(label)
    }
    return v === label
  }
  function canSubmit(): boolean {
    return args.questions.every((q) => {
      const v = answers[q.question]
      if (q.multi_select) return Array.isArray(v) && v.length > 0
      return typeof v === "string" && v.length > 0
    })
  }
  function handleSubmit() {
    if (disabled || !canSubmit()) return
    // For "Other" picks, replace the sentinel with the typed text so
    // the agent sees the actual content, not the literal "Other".
    const final: Answers = {}
    for (const q of args.questions) {
      const v = answers[q.question]
      const customText = otherText[q.question]?.trim()
      if (q.multi_select && Array.isArray(v)) {
        final[q.question] = v.map((label) =>
          label === OTHER_LABEL && customText ? customText : label,
        )
      } else if (typeof v === "string") {
        final[q.question] =
          v === OTHER_LABEL && customText ? customText : v
      }
    }
    onSubmit(final)
  }

  return (
    <div className="flex justify-start">
      {/* kami: same single accent as ConfirmationCard. Differentiation
          between "permission ask" and "agent question" comes from the
          icon and the form, not a second chromatic hue. */}
      <div className="max-w-[80%] w-full rounded-md border border-primary/40 bg-accent text-sm">
        <div className="flex items-start gap-2 px-3 pt-3">
          <HelpCircle className="h-5 w-5 text-primary mt-0.5 shrink-0" />
          <div className="text-xs text-muted-foreground">
            The agent needs your input. Pick {args.questions.length === 1 ? "an option" : "options for each question"} below.
          </div>
        </div>
        <div className="flex flex-col gap-4 p-3">
          {args.questions.map((q) => (
            <div key={q.question} className="space-y-2">
              <div className="flex items-start gap-2">
                <span className="rounded-sm bg-primary text-primary-foreground px-1.5 py-0.5 text-[10px] font-mono uppercase">
                  {q.header}
                </span>
                <div className="font-medium text-sm flex-1">{q.question}</div>
              </div>
              <div className="flex flex-col gap-1.5">
                {[...q.options, { label: OTHER_LABEL, description: "Free-form answer" }].map(
                  (opt) => (
                    <button
                      key={opt.label}
                      type="button"
                      onClick={() =>
                        q.multi_select
                          ? toggleMulti(q, opt.label)
                          : pickSingle(q, opt.label)
                      }
                      disabled={disabled}
                      className={cn(
                        "rounded-md border px-3 py-2 text-left text-xs transition-colors",
                        isPicked(q, opt.label)
                          ? "border-primary bg-primary/10"
                          : "border-input hover:bg-secondary",
                      )}
                    >
                      <div className="font-medium">{opt.label}</div>
                      <div className="text-muted-foreground text-[10px] mt-0.5">
                        {opt.description}
                      </div>
                    </button>
                  ),
                )}
                {isPicked(q, OTHER_LABEL) && (
                  <input
                    type="text"
                    value={otherText[q.question] ?? ""}
                    onChange={(e) =>
                      setOtherText((prev) => ({
                        ...prev,
                        [q.question]: e.target.value,
                      }))
                    }
                    placeholder="Type your answer"
                    disabled={disabled}
                    className="rounded-md border border-input bg-background px-2 py-1.5 text-xs"
                  />
                )}
              </div>
            </div>
          ))}
          <Button
            type="button"
            size="sm"
            disabled={disabled || !canSubmit()}
            onClick={handleSubmit}
          >
            Submit answers
          </Button>
        </div>
      </div>
    </div>
  )
}
