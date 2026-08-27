# The four questions

Drafts for the submission form. Edit into your own voice before pasting.

---

## 1. What broke?

**Honest answer: I have no SuperDocs bugs to report, because I never got an
account set up.** I built Task 1 and ran out of runway before Task 2, so I never
used the product on real work and never touched the API or MCP surface. Reporting
bugs I did not hit would be worth less than nothing to you.

What did break was mine, and it is worth saying because two of them cost me the
rest of the round:

- **My machine's disk hit 100% mid-build.** PostgreSQL stopped being able to
  write, a test suite that normally runs in 30 seconds took 18 minutes, and one
  concurrency test failed. I read that failure as a race condition and went
  looking for a locking bug that did not exist. The real cause was `df` showing
  215 MB free. **Lesson: check the substrate before you debug the logic.**
- **The project was never its own git repository.** It had been sitting inside
  my home directory's git working tree for the entire build, and my own progress
  notes asserted the opposite. I found this with about two hours left. Everything
  I had built had nowhere to be submitted to.
- **A documented command that did not exist.** `make demo` called a CLI module I
  had never written. The README would have shipped claiming a one-command setup
  that fails on the first try for anyone who cloned it.

If any of this is useful as a signal about the task itself: the thing that saved
me was writing the invariants as failing tests first. When I finally had time
pressure, I never had to wonder which parts of the system were real.

---

## 2. If you were running this company, what one number would you watch every morning?

**Approved edits per active document, per week.**

Not signups, not documents uploaded, not operations consumed. Here is the
reasoning.

SuperDocs' bet is that editing is the unsolved half and that review is what makes
an agent trustworthy in someone's real document. That bet is proven or disproven
by exactly one behaviour: **a person looked at a proposed change and pressed
approve.** Every weaker metric can go up while the product is failing. Uploads
measure curiosity. Operations consumed measure *attempts*, which rise when the
agent is bad and the user retries. Retention measures habit but lags by weeks.

Approved edits per active document per week is the only number that moves only
when the product does the job: the agent proposed something, a human read it, and
the human agreed. It also decomposes cleanly when it drops — either proposals
fell (retrieval or instruction-following) or the approval *rate* fell (edit
quality). Those are different teams' problems, and the metric tells you which.

The number I would watch beside it, not instead of it: **rejected edits per
approved edit.** If approvals are climbing while that ratio is climbing too, you
are training users to rubber-stamp, and the review layer has quietly become
theater.

---

## 3. Name five features you would build next, in order.

1. **Change propagation across a document set.** One instruction, applied
   consistently to every document that shares the fact, each edit reviewed
   separately. This is the difference between a document tool and a documents
   product, and it is the thing every use case in my Task 3 list actually needs.
2. **Rule packs as a first-class object.** A playbook, a style guide, a house
   format — uploaded as data, versioned, applied to any document. Configuration
   over code is one of your own stated standards, and it turns every new vertical
   into a data change instead of a roadmap item.
3. **A diff that a non-technical reviewer trusts.** Grouped by meaning rather
   than by range, with the source of each change visible. The review layer is the
   product's whole safety claim, and it is only as good as whether a busy person
   can scan it in thirty seconds.
4. **Deep host integrations — Google Docs and Word first.** People do not want a
   new place to keep documents. Write back range by range through the host's own
   API so that anything untouched is provably untouched.
5. **A durable, resumable job surface.** Long operations that survive a dropped
   connection, with a job id you can come back to. Everything above becomes
   unusable at scale without it.

**What I would drop to make room:** anything that widens the surface before the
review layer is excellent. Concretely — extra export formats, extra model tiers,
and any move toward being a place documents *live*. Your own rails already say
you are not becoming a document management system; I would hold that line hard,
because it is the cheapest way to stay focused.

**Frictions I would fix immediately, from reading the docs and the task
document:**
- **Proposed-change content arriving as a JSON-encoded string that needs a second
  parse.** Your own task document warns integrators about this, and calls it the
  single most common reason people see empty diff cards. A warning in a PDF is a
  workaround; the fix is to return an object. If it must stay for compatibility,
  ship it under a new field and deprecate the old one.
- **Thirty seconds to several minutes with no visible progress.** The task
  document says the correct read is "still processing, not a crash" — which means
  users are reading it as a crash. Even a coarse stage indicator would convert
  that from an abandonment into a wait.
- **Latency generally**, which you already name as the next thing you are working
  on.

---

## 4. How would you build development and GTM operations so they run themselves?

The honest frame first: **agents doing the work of 20–100 people is a claim about
verification throughput, not about generation throughput.** Generating a hundred
pull requests a day is easy and already possible. The bottleneck is that a human
has to trust each one. So every loop below is designed around the check, not the
worker.

**The shape.** Each loop is: a trigger, an agent with a narrow brief, a
*machine-checkable* definition of done, and a named human who owns the exception
queue. If a loop's output cannot be checked by something other than an LLM's
opinion, it does not get to run unattended.

**Development loops**
- *Signal → ticket.* Bug reports, support threads and error rates get clustered
  into deduplicated tickets with reproductions attached. Check: does the repro
  actually fail on main? An agent that cannot produce a failing test does not get
  to file a ticket.
- *Ticket → PR.* Agent writes the failing test first, then the fix. Check: the
  test failed before and passes after, the rest of the suite is green, and the
  diff touches only files the ticket named. **Fully automated up to the PR; never
  past it.**
- *Review.* A second agent that did not write the code reviews it — the brief's
  own "fresh pair of eyes" point, and my experience on this task matches it. A
  verifier that is not the implementer catches what the author structurally
  cannot.
- *Release.* Automated on green, with automatic rollback on error-rate regression.
  Humans steer the decision to ship a *category* of change, not each instance.

**GTM loops**
- *Use-case research → ICP list → tailored demo asset.* The demo is the artifact:
  the product doing the actual thing on a document from that industry. Check: does
  the generated demo run end to end without a human fixing it? If not, it never
  reaches a prospect.
- *Content.* One durable piece per real customer problem, not volume. Check: a
  human subject-matter read before anything publishes. This is where I would keep
  the most human involvement, because a wrong claim about your product is
  expensive in a way a wrong line of code is not.
- *Outreach stays human.* Your task document says you do all outreach yourselves
  and that contacting anyone on your behalf fails the task. I would keep that as a
  standing rail for agents too — agents prepare, humans send.

**Where humans stay, permanently:** anything irreversible or outward-facing.
Production data migrations, pricing, public claims, and any message that reaches a
customer. Also the exception queue for every loop above — the queue *is* the job.

**What breaks first, and I would design for it now:**
1. **Review capacity.** Not agent quality — the human queue. Once approvals
   outpace attention, people start rubber-stamping, and the whole safety story is
   theater. Measure approval *latency* and rejection rate; when rejection rate
   goes to near-zero, that is a warning, not a win.
2. **Silent context drift.** Agents work from stale docs and confidently produce
   coherent, wrong work. The mitigation is machine-checkable definitions of done,
   because those fail loudly.
3. **Duplicated and conflicting work.** Two agents fixing the same thing
   differently. Needs the same thing my Task 1 system needed: idempotency keyed on
   the work, not on the worker, and a lock around anything that commits.

I built a small version of exactly this in Task 1 — durable journal, human gate,
per-item approve/reject, and proof that untouched work stayed untouched. The
opinions above are downstream of having actually had to make those parts hold.
