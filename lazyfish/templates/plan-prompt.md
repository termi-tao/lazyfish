# Design brief for {{ ticket.key }}

You are producing a written plan, not an implementation.

Read `CLAUDE.md` in this directory first: it holds the ticket text, the
discussion around it, this repository's conventions, and mechanical search hits
that may or may not be relevant.

## Only the plan file leaves this worktree

You may read anything here, and you may write anything here. Nothing is locked.

But when this stage ends, **one file is taken out of this worktree and kept: the
plan.** Everything else is left behind and carries no weight afterwards. The next
stage does not start from this directory; it starts from the recorded baseline
commit plus the plan.

So writing the implementation here is not forbidden — it is simply wasted. It
will not be reviewed, it will not be built on, and it will not reach a branch.
If a change seems necessary to understand the problem, make it, read what you
needed, and describe the conclusion in the plan; that is the part that survives.

One consequence worth stating plainly: a plan that arrives with working code
attached is harder to judge honestly than a plan on its own, because the code
makes the plan look settled. The plan is what is being reviewed. Make it good
enough to be reviewed alone.

## Output

Write a single JSON object to `{{ plan_path }}`. It must validate against the
schema reproduced at the end of this file.

The keys are fixed and English, exactly as the schema spells them. Prose values
should be written in English by default, so that a plan can be read by anyone
who might pick the ticket up; a team that works in another language may write
the values in that language instead. lazyfish does not check the language of
values.

## Three rules that are checked and will fail the plan

1. `assumptions`, `alternatives_considered` and `open_questions` must each
   contain at least one entry. An empty array is read as "this was not
   considered", never as "there was nothing to consider". If an option was
   obvious enough to reject in one line, record it in one line. If a question
   turned out to have an answer, keep the question and put the answer in it.

2. `needs_human` must be `true` whenever `confidence` is `"low"` or
   `open_questions` is non-empty. Since a freshly written plan always has open
   questions, this field records that a person has not reviewed the plan yet,
   not how confident you feel about it.

3. Every `changes[].file` whose `action` is not `"create"` must name a path that
   actually exists in this worktree. Check before writing it down; a plan that
   proposes editing a file that is not there has misunderstood the codebase.

## What makes an acceptance criterion acceptable

Each entry in `acceptance_criteria` must be observable: something a person can
run or look at, with an unambiguous result. "Error handling is improved" is not
a criterion. "Requesting an unknown ticket key exits non-zero and prints the key"
is one.

## Schema

```json
{{ schema_json }}
```
