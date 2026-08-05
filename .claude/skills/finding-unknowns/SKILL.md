---
name: finding-unknowns
description: Surfaces and closes gaps between what the user actually wants (the map) and the codebase/real-world constraints (the territory) before, during, and after a task. Use at the start of any non-trivial new project, feature, or unfamiliar area of the codebase — especially when the user seems unsure what they want, when the domain is unfamiliar to them, or when a task is large/ambiguous enough that a wrong guess would be expensive to undo. Also use mid-implementation when an edge case forces a deviation from plan, and after implementation to help the user verify the result. Covers: blind-spot passes, brainstorming/prototyping, interviews, reference-based specs, implementation plans, implementation-notes logs, pitch/explainer docs, and comprehension quizzes.
---

# Finding Unknowns

Summary of Thariq's "A Field Guide to Fable" — the bottleneck on agentic work is usually not model capability, it's unresolved gaps between the user's prompt (the map) and the actual codebase/constraints (the territory). Treat every task as an exercise in finding those gaps cheaply, before they get expensive to fix.

## The four kinds of gaps

- **Known knowns** — already in the user's prompt.
- **Known unknowns** — the user knows they haven't decided yet. Resolve with an **interview**.
- **Unknown knowns** — obvious-in-hindsight criteria the user would recognize but can't state up front (e.g. visual taste). Resolve with **brainstorms/prototypes**.
- **Unknown unknowns** — things the user hasn't considered at all. Resolve with a **blind-spot pass**.

Don't over- or under-specify: too much detail makes Claude follow instructions past the point where a pivot was warranted; too little makes Claude fall back to generic industry defaults that may not fit. The fix in both cases is closing gaps explicitly, not adding more or less instruction.

## When to reach for which technique

**Before implementation**
- **Blind-spot pass**: user is new to this part of the codebase or domain. Explicitly search the codebase/history and name the unknown unknowns and relevant prior art back to the user, calibrated to their stated experience level.
- **Brainstorm/prototype**: the requirement involves taste or "I'll know it when I see it" criteria. Produce a few genuinely different options (e.g. an HTML mock with fake data, or 3-4 divergent approaches) before wiring up real state/backend.
- **Interview**: ambiguity remains after brainstorming. Ask one question at a time, prioritizing questions whose answer would change the architecture — not low-stakes cosmetic ones.
- **Reference**: the user can't easily describe the target but can point at existing code/design that embodies it. Read the reference's actual implementation (not just its surface behavior) and port the semantics.
- **Implementation plan**: before writing code on anything non-trivial, write a short plan that leads with the parts most likely to change on review (data model, type interfaces, UX/API surface) and puts mechanical/boilerplate work last.

**During implementation**
- **Implementation notes**: for longer or riskier work, keep a running log of decisions and deviations from the plan. When an edge case forces a choice, take the conservative option, log it, and continue rather than stalling.

**After implementation**
- **Pitch/explainer**: package prototype + plan + implementation notes into one artifact for getting buy-in from others.
- **Quiz**: before the user merges, give them a short report on what actually changed and a quiz to confirm they understand the behavior — don't just rely on skimming the diff.

## How to apply this automatically

At the start of a new, unfamiliar, or high-ambiguity task:
1. Gauge which of the four gap types is most likely dominant (unfamiliar codebase → blind-spot pass; vague taste-based ask → brainstorm; the user clearly has unresolved open questions → interview).
2. Say explicitly which technique you're using and why, in one sentence.
3. Don't run every technique on every task — pick the 1-2 that match the actual ambiguity, then proceed to implementation once gaps are closed enough to act.
4. Prefer a small HTML/Markdown artifact over a chat wall-of-text when the output is meant to be reviewed or reacted to (mocks, plans, blind-spot summaries, quizzes).
