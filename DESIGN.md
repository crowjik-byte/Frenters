# Design Note — Human-Directed Dialogue App

*New repo, new Streamlit app. Not a revision of the existing philosophy
dialogue app, which stays running untouched for the auto-run / belief-
revision work.*

---

## What this is

A dialogue tool where **the human drives every turn**. No auto-run loop, no
rounds, no round-robin. The human asks a named agent something; that agent
responds; the human decides who speaks next and what they're responding to.

Descended from two things: the `philosophy_dialogue_gui` engine (perspective
generator, moderator, summarizer, model plumbing) and the browser-based
`dialogue.html` artifact (manual turn-taking, agent library, saved
transcripts). This takes the engine from the first and the interaction model
from the second.

**The point of the manual loop:** the human is a participant in the thinking,
not an operator of a machine that thinks. Each turn is a decision about where
the dialogue should go next, which is where most of the value of dialogue
actually lives.

---

## Decisions

### 1. The agent is four strings

```python
{
    "name": str,          # unique, no spaces (underscores)
    "persona": str,       # 2nd person, agent-facing — used as system prompt
    "description": str,   # 3rd person, serves BOTH the human roster page
                          # and the roster block other agents see
    "kind": str,          # "perspective" | "utility"
}
```

No `provider`, no `model`, no `max_tokens`, no `effort`. An agent is purely
textual, so one saved today still works when the model changes next year.

**Two descriptions, not three.** An earlier version of this design had a
separate agent-facing "blurb" and human-facing description. Collapsed: the
audiences want the same thing (who is this, what do they hold, what do they
press on), and a paragraph written for a person reads fine to a model. Three
texts per agent is three texts that can disagree about what the agent is.

**Never give an agent the full `persona` of another agent.** It starts
modelling and pre-empting them — arguing against a position nobody has stated
yet. The roster block carries `description` only.

### 2. `kind` is behavioral, not cosmetic

`utility` agents (moderator, summarizer) operate *on* the dialogue.
`perspective` agents operate *in* it. Consequences the code acts on:

- Utility agents don't appear in the roster block as positions other agents
  can be curious about.
- The summarizer enumerates perspective agents only when listing positions.
- `validate_roster` requires at least one *perspective* agent, not just one
  agent. A roster of moderator + summarizer otherwise passes and then has no
  dialogue to moderate.

### 3. Domain taxonomy — deferred

Deliberately not building neuroscience / philosophy-of-mind / contemplative /
economics categories yet. Revisit once there's a real body of agents to
organize; the shape of the problem will be clearer then than it is now.

**But write provenance from day one.** On save, record the question that
generated the agent, the timestamp, and which agents it ran alongside. Domain
tags can be backfilled later by one batch call over the whole roster.
Provenance cannot be reconstructed once it's lost. That asymmetry is what
decides what defers cheaply.

Likely true, worth testing before building the taxonomy: what makes a
50-agent roster hard isn't *finding the economics ones*. It's duplicates
(three adjacent generation runs give three slightly different
phenomenologists), quality (which ones actually produced something), and
pairing (which combinations generated real disagreement). All three are
answered by provenance metadata, which costs nothing to maintain.

### 4. The human is a labeled participant

Agents already get labeled turns. A human turn is just another labeled turn.
**Nothing in `ask_agent` branches on speaker type.**

Label with the person's actual name, not "Human" or "User" — a label naming a
category invites the agents to treat it as a category.

### 5. Routing is visible

Every human turn carries an `addressee`, and the transcript renders it:

```
Nathan → Phenomenologist: what do you make of that last point?

Phenomenologist: ...

Nathan → Enactivist: respond to what Phenomenologist just said about asdf
```

Agent turns have no addressee — an agent wanting a specific person's take
just says so in prose.

The alternative (strip the addressee, render clean prose only) was rejected:
some directives don't survive stripping ("Agent Y, I didn't understand this
point" has the addressee load-bearing inside the sentence), and it would need
two input fields.

**Consequence, accepted deliberately:** the orchestration is on the table.
Agents see who was called on, may address the human as the one running
things, may hold back when someone else was named. That's an accurate
representation of what's happening, and it's what makes the moderator
summonable without special-casing.

**One string, appended once.** The human's message goes into the transcript
as a turn, *then* the addressed agent is called with the transcript already
containing it. Don't also pass it as the agent's `user_message` — that's a
double-feed.

### 6. Roster block with curiosity

Every perspective agent's system prompt carries a roster of the other
participants (name + `description`), including the human. Agents may name a
specific participant they'd like to hear from.

**The instruction is permissive, not obligating.** Phrased loosely, every
turn ends with "I'd be curious what X makes of this" — which is noise, and
quietly inverts control: the human ends up working a queue of agent-generated
requests instead of directing. Watch the first runs for it degenerating into
a politeness ritual; the fix is tightening the sentence, not removing the
roster.

**Free by-product worth reading:** who gets invited in, by whom, and who
never gets named at all. That's a signal about where an agent thinks the live
tension is, cheaper to read than the content of the request. An agent nobody
nominates is either genuinely peripheral to the question or is being *treated*
as peripheral — different things, distinguishable by reading what it actually
said. Hypothesis worth testing rather than expecting: with a roster holding,
say, a cognitive neuroscientist and a Dzogchen practitioner, does curiosity
route disproportionately toward the one whose vocabulary reads as legitimate?
Thin evidence over a handful of turns, and there are boring explanations
(recency, concreteness) — but the tally is free and the asymmetry is
invisible in a straight read-through.

### 7. Moderator: summoned, and it obeys

Kept from the old engine, but no longer automatic. It speaks when addressed,
like any other agent. `round_num` is gone; the persona's round scaffolding
("after each round", "two rounds earlier", "in a given round") is rewritten
for call-based operation.

**`MODERATOR_ENGAGEMENT_INSTRUCTIONS_OBLIGATED` is dropped.** It existed
because an unbidden observation gets gestured past, so agents had to be forced
to address a naming of their own gap. Human routing replaces it: if nobody
engages, type "Agent X, respond to what the moderator just said." Obligation
becomes a per-case decision rather than a standing rule, which is strictly
better — the blunt version couldn't tell which observations deserved a
response. The non-obligating engagement instruction stays; agents still need
to know what the thing is, since its turns are in the visible transcript.

**Open item — the summons frame.** A summoned moderator answers *you*, and
every summons carries a frame. "What do you see going on" is near-neutral;
"is agent X dodging?" hands over both the target and the verdict and asks the
moderator to supply the justification. This is exactly the failure the
separate moderator-pushback log exists to track, and on-demand calling makes
it the normal mode rather than an occasional accident.

Decided mitigation is **interface, not architecture**: the roster page states
plainly what the moderator does (names what it observes; does not
adjudicate), so summonses get phrased better at the source. Explicitly
rejected: making the moderator's task fixed and treating the summons as mere
context, which would have meant it could decline to answer what was actually
asked.

Still open, not yet decided — one optional persona line: *when a summons
presupposes a finding, name that before answering.* Obeys fully, still does
the observing job on the human's turn as well as the agents'. Try it; cut it
if it reads as evasive.

Strengthen in the persona, don't leave as-is: *"Do not search for a defect,
insight, tension, or transformation merely because you have been given a
turn."* An automatic moderator gets turns for free. A summoned one has been
asked, by a person, for a reason — so "I see nothing worth naming" is now
both costlier to say and more informative.

**Watch for:** nominating the observer as a deflection. An agent under
pressure asking for a moderator read instead of answering is a real move. At
least self-correcting, since it's the sort of thing the moderator would name.

### 8. Summarizer: utility agent, default instructions, no configuration

One function, one default instruction block, a "summarize so far" control.
Not a configurable bucket with its own model/effort/notes-expander like the
old Advanced tab.

Changes from the old default instructions:

- **Item 6 removed** (whether an agent's contributions look shaped by its
  underlying model rather than its persona). One model per session means that
  can't vary; leaving it in has the summarizer hunting a dead variable.
- **Item 1 scoped** to perspective agents.
- Prompt preamble updated: the transcript is no longer "between AI agents,"
  the human is in it.

The instructions are already round-agnostic ("the exchange" throughout), so
nothing else needed changing.

### 9. Model config

- **`session_model`** — one model for dialogue turns, moderator, and
  summarizer. Set per session, not per agent.
- **`PERSPECTIVE_GENERATOR_MODEL`** — independent. Generating a good slate is
  a one-shot JSON task where a stronger model is cheap and pays off once;
  dialogue turns are where the cost lives. Generating with Opus and running on
  Sonnet is now just a thing you do.
- **`max_tokens` stays per role.** A summary of a long transcript needs more
  room than a dialogue turn; a moderator observation needs less than either.
  One model doesn't mean one budget.
- **No `provider` anywhere.** Anthropic only. `call_model` keeps no provider
  parameter — if a second provider ever matters, that's a contained change in
  one function.

Dead with the above: `DEFAULT_SUMMARY_PROVIDER`, `DEFAULT_MODERATOR_MODEL`,
`_resolve_agent_settings`, per-agent override toggles, all model pickers
except the generator's.

### 10. Persistence

**Two different problems with different costs.**

*Shared read* — curated agent/question sets anyone can load. Git-committed
JSON in `sets/`, read-only, survives every redeploy by construction. Zero
backend. This is the existing `DEMOS_DIR` mechanism and it can ship on day
one.

*Shared write* — people save their own agents and transcripts and those
persist. Needs a real database. Streamlit Community Cloud does not guarantee
persistence of local file storage and may delete it at any time; local SQLite
or a JSON file in `data/` gets wiped on redeploy, restart, or sleep.

**Not yet decided.** Leading option: Community Cloud + hosted Postgres (Neon
or Supabase free tier), credentials in `st.secrets`. Alternative: Render /
Railway / Fly.io with a mounted volume and local SQLite, a few dollars a
month, fine at single-instance scale. (Render's *free* tier has no persistent
disk and sleeps — doesn't help.)

Until it's decided, the app talks to a **storage interface**, with a local
JSON implementation for development. Swapping in Postgres is then one class.

**`schema_version` on every saved record** — agents, transcripts, sets. One
integer now; without it, the first change to the agent shape leaves you
guessing which stored rows predate it.

### 11. Multiple human participants — async, built for from the start

The use case: send a link to a dialogue in progress and say "have a look, ask
something if you want." Not live co-presence — nobody is watching turns
appear in real time. These exchanges are slow by nature; reading four
positions and deciding who to press next isn't a real-time activity.

Async means **no polling timer and no concurrency machinery**. Append-only
turns mean near-simultaneous additions both land. Streamlit is a fine fit for
this; it is only live co-presence that it genuinely fights.

Three things must be right from the start. All are cheap now and annoying to
retrofit:

1. **Separate "append a turn" from "invoke an agent."** The obvious
   implementation is one submit handler — human types, append, call the
   addressed agent. With two humans a turn can be human-to-human, with no
   API call at all. If those are welded together, splitting them later
   touches everything. Kept separate, multi-human is just "sometimes the
   addressee isn't an agent."
2. **Store the transcript as an append-only list of turn records, not a
   rendered string.** Rendering happens at read time. This is also what the
   curiosity tally and provenance want anyway.
3. **Participants have ids, not just display names.**

The four-field agent shape and the labeled-turn transcript already
accommodate a second human almost for free — that is the payoff of §4's
decision that nothing branches on speaker type. A second person is another
roster entry.

### 12. Access model — two link types, one mechanism

A query-param token checked on load:

- `?d=<dialogue-uuid>` — **participant link.** Read and append turns on that
  one dialogue. Nothing else.
- `?k=<app-token>` — **app link.** Create dialogues, generate perspectives,
  manage agents.
- No token — landing page.

A participant link must grant rights on *that dialogue only*, not roster
access. Cheap to build now, awkward once everything reads from one global
roster.

Keep the app itself **public, scoped by link**. A Community Cloud private app
puts recipients through Google OAuth or single-use emailed links, which is
exactly the friction that turns "ask something if you want" into "never
mind." An app token isn't real security, but a bare Community Cloud URL is
public and findable, and a token filters drive-bys — which at family scale is
the actual threat model.

**This also gives usernames without auth.** Issue a *different* app token per
person, each mapping to a display name in a small table. Nobody logs in; they
click their link and the app knows who they are. Same option for participant
links: per-recipient if you want to know who asked what, shared if you don't.

So public vs. personal agent libraries need no account system. Two fields on
the agent record:

```python
"visibility": str,    # "personal" | "public"
"created_by": str,    # participant id
```

Personal means "doesn't appear in the public library," not anything
cryptographic. Soft, sufficient, and it fails open rather than dangerously.

**Write both fields in the first migration even if the UI ignores them.**
Same asymmetry as `schema_version` and provenance: a nullable column costs
nothing today and is a migration across live data later. The UI can start as
one global library and lose nothing.

**Default `visibility` to `personal`**, with public as a deliberate
promotion. A shared library puts other people's generated agents next to
carefully built ones — fine at five agents, and at fifty it's the duplicate
problem from §3 arriving faster.

### 13. Fresh repo — validate strictly

No legacy rows exist, so no fallback handling for missing `kind` or
`description`, and no ignore-unknown-keys on agent load. This is the only
moment strictness is free; once saved agents sit in a shared database,
tightening a field means a migration.

(Set files keep an explicit key allowlist, but for a different reason —
hand-edited JSON with a typo'd key, which is human error rather than version
skew.)

---

## Next iteration — confirmed, build together

**Status: N1–N10 are DONE** (engine + app, tested with stubbed model
calls; not yet run against a live dialogue). They were one defect in three
places: every component that needed the roster's actual state either lacked
it, had it in a misleading form, or measured it by the wrong proxy. Details
under each item below. The list is clear; what follows is the record of why each is the way it is.

Not open questions. Decided; deferred only so the first real test finishes
before the code changes under it.

### N1. ~~Add a perspective mid-dialogue~~ — DONE

Currently the roster is snapshotted into the dialogue at creation and there
is no path to add to it. Wanted, and confirmed by hitting the same wall
twice — in this app's design and independently while testing the in-session
HTML artifact. The case is one the exchange itself creates: a dialogue
reveals it needs a perspective nobody thought to include, and the only
current remedy is starting over.

Design points to settle when building:

- **The new agent joins without a history.** It reads the transcript as a
  latecomer. Whether it's told plainly that it joined at turn N, or simply
  handed the transcript like everyone else, is a real choice — the first is
  honest and may license "I've only just arrived"; the second is uniform
  with how every other agent is prompted.
- **The existing agents' roster block is stale.** Their system prompts are
  built per call from the current roster, so this resolves itself on their
  next turn. But the *transcript* shows the new name appearing with no
  introduction, which is fine and is what happens when someone walks into a
  seminar.
- **Where the agent comes from**: the original sweep's candidate list (still
  stored on the dialogue in `sweep`), the personal library, or freshly
  written. All three are wanted; the sweep list is the cheapest since it's
  already there.
- **Removing** an agent mid-dialogue is a different and weaker case — the
  transcript already contains their turns, so removal only stops future
  ones. Probably not worth building.

### N2. ~~Confirm the selection before personas are written~~ — DONE

Currently `select_perspectives` picks the slate *and* writes full personas in
one call, so the first thing shown is a finished product. Wanted instead:
**pick → show which ones, with the rest of the candidate list still visible →
concur or change → then write personas.**

Split into two engine functions:

- `choose_perspectives(question, candidates, count)` → names only, plus
  `rationale` and `near_misses`. Short output, cheap call.
- `write_personas(question, chosen, candidates)` → full `persona` and
  `description` for the agreed names, all in one pass.

Two reasons this is more than a UI change:

- **A swap currently costs a full rewrite.** Changing one perspective after
  selection goes through `refine_perspectives`, which returns the whole slate
  and rewrites all five personas. Confirming first means a swapped-in
  perspective gets its persona written *in the same pass* as the others —
  the personas are composed with awareness of each other, so the tensions
  are complementary by construction rather than retrofitted.
- **The overlap counter lands at the decision point.** How many of the
  model's own stated first instincts survived into the slate is exactly the
  number you want before committing, not after.

Keep `select_perspectives` as a thin wrapper over both for callers that
don't want the confirmation step.

### N3. ~~Start a dialogue from the library~~ — DONE

`page_new_dialogue` only creates a dialogue via sweep → select → commit.
Agents are saved to the library on commit (with `source_question`), but
there is no path to *use* them — so continuing a question with the same
slate means redoing the sweep.

The case that surfaces it: a long dialogue degrades in quality (agents start
summarizing the exchange back and hedging across everything already said),
so you summarize, start fresh, and carry the same roster forward. Same
shape as starting a new chat. Not a cost problem — a 30-turn dialogue is
single-digit dollars and nowhere near the context limit — a *quality at
length* problem.

Wanted: pick a roster from the library, optionally seeded with a prior
dialogue's summary as the opening turn.

**Built as predicted.** `pick_names()` in `app.py` is one picker over three
sources (stored sweep candidates, library, hand-typed with a note), and
N1, N2, N3 and N8 are all thin wrappers over it. `picked_with_notes()`
carries a hand-typed note through to the persona writer, so a perspective
nobody swept is written from the person's own one-line gloss rather than
from the name alone.

### N4. ~~The curiosity tally under-counts~~ — DONE

`curiosity_tally` / `never_named` replaced by `engagement_tally` /
`never_engaged`, returning three separate signals:

- `named` — an agent used another participant's name.
- `quoted` — an agent reproduced a run of another's earlier words (7-gram
  match, floor tuned so short shared phrases don't fire) without naming
  them. This is the case the old version missed entirely.
- `early` — a name used before that participant had taken any turn.

**`early` is deliberately left ambiguous, and testing forced that.** The
first version called it `phantom` and treated it as a hallucination
counter. But the real transcript contains both shapes: Predictive
Processing's *"I would push directly on that with Theravada"* before
Theravada had spoken (a legitimate forward nomination — exactly the
behavior the roster block is for) and Theravada's *"SomaticExperiencing has
pressed on..."* (the false attribution). Structurally identical; the
difference is attributing a past speech act versus requesting a future one,
and no reliable mechanical test separates them. So it flags turns to read
rather than asserting an error.

Still missed: engagement by paraphrase that neither names nor reproduces
words. Stated in the UI caption rather than papered over.

**Found in the first real dialogue.** Predictive Processing addressed
Theravada by name — registered. Theravada then put a question to Predictive
Processing by quoting its earlier text without using its name — not
registered.

This is worse than the over-counting already documented in the code, because
it corrupts the one reading claimed as reliable. "Never named by anyone" was
offered as meaning *nobody engaged with them*. It actually means *nobody
used their name*, and an agent can be the most engaged-with participant in
a dialogue while showing up as never named. **Downgrade that claim in the
code comment and in the UI caption.**

Two different things are measurable and were being conflated:

- **Explicit nomination** — naming a participant and saying what you want
  from them. What the roster instruction actually asks for. What the tally
  measures.
- **Directed engagement** — a question aimed at a specific participant,
  however phrased. What the tally was described as measuring.

**Open, and possibly more interesting than the fix:** whether
naming-versus-quoting is a distinction worth tracking rather than noise to
correct for. Naming treats someone as a participant with standing;
engaging with their words without naming treats what they said as material.
Those may be the same act in different clothes, or not. One observed pair
is a hypothesis, not a finding — recency and the shape of the specific
question explain it equally well. But it is the kind of asymmetry that is
invisible reading a transcript straight through, which is what the tally
exists for.

Approaches, if it gets fixed at all: match against prior turns' text to
catch quotation (fragile); or have a model extract nominations from the
transcript (reliable, costs a call, and is Claude scoring Claude — same
structural problem as the other logs in this project, so it would be a
pointer to transcripts worth reading rather than a measurement).

### N5. ~~The roster block gets read as contributed content~~ — DONE

**Found in the first real dialogue, and confirmed verbatim in the
transcript.** Theravada wrote: *"Somatic_Experiencing_Levine has pressed on
whether this is genuine completion or a dissociative override."*
Somatic_Experiencing_Levine had never taken a turn. Its roster
`description` reads: *"asks whether apparent non-identification might be a
dissociative override rather than genuine completion."* The hallucinated
speech act is a near-verbatim restatement of the description — mechanism
confirmed, not inferred.

Note the contrast in the same transcript: Krishnamurti wrote *"Predictive
Processing put it well when it said the meta-frame of the witnessing
meditator is still a prior"* — which Predictive Processing had actually
said, accurately attributed. Agents read the transcript correctly. The
failure is specifically that roster descriptions are indistinguishable
from transcript content, not a general attribution problem.

Not an invention from nothing. `build_roster_block` gives every agent the
others' `description` fields, and those are written as *"Holds that… Presses
other perspectives on whether…"* — formally indistinguishable from a summary
of positions already argued. So an agent has two sources of information
about a participant (the transcript, and a paragraph that reads like a
précis of their view) and nothing in the prompt marks them as different
kinds of thing.

This is the failure the description-not-persona rule was supposed to
prevent, and it stopped short. The problem is not detail level; it is the
**absence of any marker that these are positions held rather than things
said**.

**Fixed, both parts.** `ROSTER_HEADER` now states that the block describes
what each participant *holds*, that only the transcript records what anyone
said, and that a participant marked silent must not be described as having
pressed, asked or claimed anything. `build_roster_block` takes a `spoken`
set and renders *(has not spoken yet)*. `ask_agent` derives that set from
the turns itself via `spoken_names()`, so no caller can forget it.

**Third contamination of the tally (see N4).** A nomination prompted by
content a participant never produced is interest in their *description*,
not in what they said. The roster block can therefore generate nominations
rather than merely enable them — which also means the curiosity signal is
partly an artifact of how the descriptions were written.

### N6. ~~Expected-tensions panel, and store the selection rationale~~ — DONE

The first run used three of five perspectives for thirteen turns —
deliberately, working one thread at a time, with the other two still
intended. So the gap isn't that the human loses track of *who* hasn't
spoken; it's that nothing keeps **why each perspective was chosen** in
view while the dialogue runs. The selector predicted that Krishnamurti and
the Stoics contradict each other on trained capacity versus sudden
non-causal seeing, and that Somatic Experiencing would press all four on
whether apparent non-identification is dissociative override. By turn
thirteen that reasoning exists nowhere on screen.

Wanted: a side panel listing the expected tensions, as a standing reminder
of what the slate was built to produce.

**Prerequisite — the rationale is currently discarded.** `create_dialogue`
stores `sweep` but not `selection_meta`. The rationale and `near_misses`
live only in session state and are lost at commit. Persist them on the
dialogue record; without that there is nothing for the panel to show.

**Ask the selector for structure, don't parse prose.** The rationale is
one blob. A panel wants pairwise entries — `{a, b, tension}` — plus
optional "presses everyone on X" entries for perspectives that cut across
the slate rather than opposing one other. That's a key in the selector's
JSON contract, which N2 is reworking anyway. Do both at once.

**Note it's a prediction.** These tensions are what the selector expected,
not what happened. A dialogue that doesn't produce the predicted
disagreement is a finding about the slate (or the selector), not a
failure by the human to cover the material — and the panel shouldn't read
as a checklist. Label it as expected, not required.

**Interacts with N3.** A dialogue with perspectives still unused is a good
candidate for carrying the roster forward rather than starting fresh.

### N7. ~~The moderator has no roster and no speaker list~~ — DONE

**Found on the first Observer run.** It reported that Theravada's question
to Somatic_Experiencing_Levine "received no response" and "dropped out of
the exchange" — treating SE as a participant who failed to answer. SE had
never taken a turn. It also did not flag Theravada's claim that SE "has
pressed on" a point, which is the N5 false attribution.

`ask_moderator` passes only the question, the human participant names, and
the transcript. No roster. So the Observer inferred SE's presence from
Theravada's reference to it — the same error as N5, propagating one layer
down: a referenced participant read as a participant who spoke.

**Fixed.** New `build_participant_ledger(agents, turns, humans)` renders
names, kinds and turn counts — no descriptions, since those are what
produced the hallucination it failed to catch. `ask_moderator` takes
`agents` and prepends it, closing with a line saying that a reference to a
silent participant points at no turn in the record and naming it is its
job. `agents` is optional; without it the old humans-only note is used, so
existing callers don't break.

**Everything else on this run was strong** and argues against touching the
persona: it caught the question changing form mid-dialogue (switching
mechanism → whether any process in time reaches outside it) with nobody
naming the shift; a concession made and then not carried through
(Predictive_Processing conceded the meta-frame point, then continued
operating the prior/error-signal framework unchanged); one word used by
two participants for possibly different referents without either noticing
("collapse"); a direct contradiction neither addressed; and four candidate
answers to the original question never compared. It marked one item
explicitly as observation rather than judgment, stayed non-directive
throughout, and did not manufacture findings under an open summons.

**Feeds the summarizer review.** The concession-without-consequence catch
is the pattern summary item 5 (rhetorical vs. logical force) is meant to
surface. Worth comparing what the summarizer produces on this same
transcript before deciding whether item 5 earns its length.

### N8. ~~Recommend a perspective, mid-dialogue~~ — DONE

Distinct from N1. N1 is adding a perspective the human has already decided
on; this is asking *what's missing* — "recommend a perspective that could
speak to the question that's come up here from a different angle," or "that
would be a useful counterweight to where this has gone."

**The trap, and it's the same one the first sweep exposed.** A recommender
conditioned on a transcript is reading a dialogue that has already
converged on a frame, so it is *more* exposed to the adversarial-traction
bias than the original sweep was, not less. There the criterion "would
reach different conclusions" silently excluded perspectives that decline
the question rather than answering it differently (Ubuntu, the
depersonalization entry — not rejected, never considered). With a live
frame on the page, "counterweight" will tend to return disagreement
*within* the frame, which is the easy thing to supply and not what was
asked for.

Two modes, worth keeping separate:

1. **From the stored sweep candidates.** Already on the dialogue record,
   no retrieval call needed, and — the real advantage — chosen before the
   frame converged, so it is partly protected against frame-capture by
   construction.
2. **Fresh sweep conditioned on the transcript.** New material, full
   frame-capture exposure. Needs the counterweight instruction written
   carefully: ask explicitly for perspectives that would question the terms
   the dialogue has settled into, not only ones that would answer
   differently within them.

**Do not implement this as an Observer function.** The obvious move is to
ask the Observer, which already reads the record and demonstrably sees what
is missing. Its persona states it has no authority to direct and that
naming is the whole job. Recommending who should join is directing. Keep
it a separate call.

### N9. ~~`refine_question`~~ — DONE

Port from the old engine. Independent of the run loop, and the question is
what the entire sweep is conditioned on — a sharper question is the highest-
leverage edit available anywhere in the flow.

### N10. ~~Reference material upload~~ — DONE

Port `extract_text_from_upload` and `prepare_reference_material`. Shared
across all agents in a dialogue, text-extraction only (no OCR). Primary use
is downloaded research papers.

**Resolved: perspective agents only.** The Observer and Summarizer do not
receive it, and that is a decision about what those agents ARE rather than
plumbing. An observer that has read the sources could say "you misread that
paper" — which is adjudicating between participants, and its persona states
plainly that it does not adjudicate. The Summarizer reports what happened
in the exchange, not how the exchange measures against the literature.
Asserted in `ask_agent`'s docstring and covered by a test, so it can't drift
back by accident.

**Also decided:** the reference block tells agents to use the material where
it bears on what they are saying and to cite it checkably, but explicitly
not to treat it as authoritative merely because it was provided — a
perspective's position may be that a source is mistaken or answers a
different question. Without that, uploading a paper quietly converts the
dialogue into an exegesis of it.

The 60k-character cap is about per-turn cost across a long dialogue, not the
context window, which is nowhere near it. Scanned PDFs raise rather than
returning empty text: a silent failure here is invisible until an agent
confidently discusses a document it never received.

---

## Still open

1. **Persistence backend** — blocks the storage layer and the deploy target.
2. **API key exposure.** Accepted for now — low volume, family scale, one
   key. Set a console spend limit as the backstop. Revisit if the app ever
   goes wider than people who are personally sent a link.
3. *(moved to N9 — decided.)*
4. **The optional moderator presupposition line** (see §7).
5. *(moved to N10 — decided.)*
6. **Review the summarizer instructions against real transcripts.** The
   default checklist was written for the auto-run app and inherited here
   with two edits (item 1 scoped to perspective agents, the dead
   model-variance item removed). Whether the rest still earns its place —
   particularly the rhetorical-vs-logical-force reconstruction, which is
   long and may be doing less than it costs — is a question for actual
   output, not inspection. `SHORT_SUMMARY_INSTRUCTIONS` is also carried over
   unexamined.

7. **A catch-up summary for someone joining mid-dialogue** — different genre
   from the analytical summary, not a shorter version of it. A newcomer
   needs orientation: the question, who's here, roughly what each has staked
   out, where it's live *now*. The existing instructions are retrospective
   and forensic; both are wrong for this.

   **The trap, and the reason this isn't just a third instruction block:** a
   catch-up summary primes the newcomer. Tell someone the sharpest
   disagreement is X and they will ask about X. But part of why you invite a
   fresh person is the question they'd ask that nobody already in the
   dialogue would think to ask — and a *good* summary is precisely what
   destroys that. So the useful version may be deliberately thinner than a
   good summary would be, or offered collapsed with the raw transcript as
   the primary path. Worth testing both ways with a real newcomer rather
   than reasoning about it.

8. **Short instructions for new users** — a first-time reader arriving on a
   `?d=` link has no idea what this is or how to take a turn. Write after a
   couple of real test dialogues, not before: the things that actually
   confuse someone are not the things you'd guess in advance. Likely needs
   to cover, at most: you're a participant like the agents are, you pick who
   answers, the Observer reports on the exchange rather than judging it, and
   nothing happens until you address someone. Keep it short enough to sit on
   the page rather than in a doc nobody opens.

---

## What was dropped from the old engine

`run_dialogue_stream`, `run_followup_stream`, `_rotate`,
`_resolve_agent_settings`, `with_movement_tracking`,
`MOVEMENT_TRACKING_INSTRUCTIONS`, `DEFAULT_GENERAL_INSTRUCTIONS`,
`DEFAULT_QUESTION_SPECIFIC_INSTRUCTIONS`, `expand_summary_instructions`,
`generate_question_specific_instructions`,
`MODERATOR_ENGAGEMENT_INSTRUCTIONS_OBLIGATED`, all provider plumbing for
DeepSeek and Cohere, all `DEFAULT_*_PROVIDER` constants, per-agent model and
token settings.

Roughly half the original file. Because this is a fresh repo the port is
copy-into rather than delete-from, so no orphaned constants or half-
referenced round logic survive by not being grepped for.
