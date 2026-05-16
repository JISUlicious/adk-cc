"""After the data-science refactor, this package keeps only
`token_counter` — the prompt-token estimator consumed by
`ContextGuardPlugin`. The permission engine, rule model, settings
hierarchy, broadening logic, and HITL confirmation prompts have
all been removed: the DS variant has no destructive tools, so the
gating layer is dead.
"""
