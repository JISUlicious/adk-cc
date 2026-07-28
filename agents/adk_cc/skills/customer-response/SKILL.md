---
name: customer-response
description: >
  Triage an inbound customer message and draft the reply — severity and routing,
  the actual answer verified against docs or code, and commitments only where
  they can be kept. Use for support queues, escalations, and complaint handling.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the technical answer was checked against real docs, code or a reproduction in this turn", "no commitment (date, fix, refund, compensation) is made that the workspace evidence supports", "severity and routing are justified by stated impact, not by the customer's tone", "anything uncertain is marked for a human rather than smoothed over"]}
---

# Customer response

Two jobs, in order: work out what this actually is, then answer it. Most bad
support replies come from skipping the first — answering the question asked
rather than the problem underneath — or from promising something the business
has not agreed to.

## 1. Triage

| Dimension | Read it from |
|---|---|
| Impact | how many users, is work blocked, is data at risk |
| Urgency | deadline, contractual response window (ask — do not assume) |
| Category | bug · how-to · billing · feature request · complaint · security |
| Route | who can actually resolve it |

Severity comes from **impact**, not volume or tone. An angry message about a
cosmetic issue is not a P1; a calm one reporting silent data loss is. Say what
you based it on.

**Security, legal threats, data-protection requests, and anything involving
personal data, minors, or a regulator go to a human immediately** — draft
nothing but an acknowledgement. Obligations and deadlines for these differ by
jurisdiction and contract; do not state them from memory.

## 2. Verify before you answer

If it is technical, check it in the workspace — do not answer from the shape of
the question:
```bash
grep -rn "<the error message>" --include=* . | head
```
Reproduce it if you can. "This is expected behaviour" is a claim; back it with
the doc line or the code path. If you cannot verify, say what you checked and
what you could not.

## 3. Draft

Structure that works: acknowledge the actual impact → what you found → what
happens next, with who and when → what they can do meanwhile.

Rules:
- **Commit only to what is agreed.** No dates, fixes, refunds, discounts,
  credits or compensation unless the workspace evidence or the user says so.
  Where a commitment is needed, write `[NEEDS APPROVAL: <what>]` in place of it.
- Match their register; do not out-cheerful a frustrated customer.
- Name the thing that went wrong plainly. Hedged non-apologies read as evasion.
- No blame directed at the customer, another team, or a vendor.
- Say what you do not know, and when you will know it.

For a genuine failure on your side: what happened, what it affected, what is
already done, what prevents recurrence — and skip that section entirely rather
than inventing a preventative measure.

## Output

```
Triage: category · severity + why · route to · SLA context [ask if unknown]
Verification: what was checked, what was found, what remains unknown
Draft reply: ready to send
Flags: [NEEDS APPROVAL: …] / [HUMAN REQUIRED: …]
Internal note: root cause if known, follow-up, whether others are affected
```
