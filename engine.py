"""
Core engine for the human-directed dialogue app.

Pure Python -- no Streamlit, no storage, no framework. Everything here takes
plain dicts and strings and returns plain dicts and strings, so the app layer
and the storage layer can both change without touching this file.

Descended from the philosophy_dialogue_gui engine, but this is a copy-into,
not an edit-down: the auto-run loop, rounds, round-robin rotation,
per-agent model config, multi-provider plumbing and the configurable
general/question-specific instruction machinery are all absent by design.
See DESIGN.md for what was dropped and why.

The central design commitment, which several things here depend on: the
human is a labeled participant like any other, and nothing branches on
speaker type.
"""

import json
import os


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

# Stamped on every record the storage layer persists (agents, dialogues,
# sets). One integer now; without it the first change to a record shape
# leaves you guessing which stored rows predate it.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# MODEL CONFIG
#
# One model per session for everything that happens *inside* a dialogue
# (agent turns, moderator, summarizer). The perspective generator is
# independent: generating a slate is a one-shot JSON task where a stronger
# model is cheap and pays off once, while dialogue turns are where the cost
# actually lives. Generating with Opus and running on Sonnet is a normal
# thing to do, not a special case.
#
# max_tokens stays per role even with one model -- a summary of a long
# transcript needs more room than a dialogue turn, and a moderator
# observation needs less than either.
# ---------------------------------------------------------------------------

DEFAULT_SESSION_MODEL = "claude-sonnet-4-6"
PERSPECTIVE_GENERATOR_MODEL = "claude-sonnet-4-6"

DEFAULT_TURN_MAX_TOKENS = 4000
DEFAULT_MODERATOR_MAX_TOKENS = 3000
DEFAULT_SUMMARY_MAX_TOKENS = 5000
DEFAULT_GENERATOR_MAX_TOKENS = 4000

# write_personas returns every persona in ONE call so they are composed with
# awareness of each other. That makes its output scale with roster size, and
# a fixed ceiling silently truncates the JSON partway through the last entry.
# Observed: seven personas ran to roughly 6,500 tokens against a 4,000 budget.
PERSONA_TOKENS_EACH = 1200
PERSONA_TOKENS_BASE = 1000
DEFAULT_REFINER_MAX_TOKENS = 4000

# Reference material is prepended to every perspective agent's system
# prompt, so it is paid for on every turn. The cap is about keeping a
# dialogue affordable and legible across many turns, not about the context
# window, which is nowhere near this.
DEFAULT_MAX_REFERENCE_CHARS = 60_000

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

KNOWN_MODELS = (
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)
CUSTOM_MODEL_OPTION = "custom..."


# ---------------------------------------------------------------------------
# API CLIENT
#
# Anthropic only. If a second provider ever matters, it is a contained
# change in this one function rather than a field on every agent.
# The caller (app.py) is responsible for putting ANTHROPIC_API_KEY in
# os.environ before anything below runs -- this module doesn't know or care
# whether it came from st.secrets, a .env file or a shell export.
# ---------------------------------------------------------------------------

_client = None


def get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def call_model(model, system_prompt, user_message,
               max_tokens=DEFAULT_TURN_MAX_TOKENS, effort=None):
    """
    One system prompt plus one user message in, plain text out.

    `effort`: one of EFFORT_LEVELS, or None to omit the parameter entirely
    and let the API use its own default.
    """
    text, _ = call_model_with_meta(
        model, system_prompt, user_message, max_tokens=max_tokens, effort=effort
    )
    return text


def call_model_with_meta(model, system_prompt, user_message,
                         max_tokens=DEFAULT_TURN_MAX_TOKENS, effort=None):
    """
    Same as call_model, but also reports whether the response was cut off by
    hitting max_tokens. The API tells us directly via stop_reason, so this
    reads that field rather than guessing from output length or shape.

    Returns (text, truncated).
    """
    client = get_client()

    kwargs = {}
    if effort is not None:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got {effort!r}")
        kwargs["output_config"] = {"effort": effort}

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        **kwargs,
    )

    # Don't assume content[0] is the text block -- models with extended or
    # adaptive thinking can return a ThinkingBlock first, which has no .text.
    text = None
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break

    if text is None:
        block_types = [getattr(b, "type", None) for b in response.content]
        if block_types and all(t == "thinking" for t in block_types):
            raise RuntimeError(
                f"{model} spent the entire max_tokens budget ({max_tokens}) "
                f"thinking and never produced an answer. Thinking tokens count "
                f"against the same ceiling as the answer itself -- raise "
                f"max_tokens to give it room to finish thinking and still reply."
            )
        raise RuntimeError(
            f"No text content block in the response -- got types: {block_types}"
        )

    truncated = getattr(response, "stop_reason", None) == "max_tokens"
    return text, truncated


# ---------------------------------------------------------------------------
# QUESTION REFINEMENT
#
# The question conditions the entire sweep, so a sharper question is the
# highest-leverage edit available anywhere in this flow -- which is why
# this is a conversation rather than a one-shot rewrite.
# ---------------------------------------------------------------------------

QUESTION_REFINER_SYSTEM_PROMPT = """
You are helping someone refine a single, precise question for a dialogue
between several AI agents reasoning from different named perspectives,
directed turn by turn by the person themselves. This is a conversation,
not a one-shot request: read what they have said so far and respond the
way a thoughtful editor would -- sometimes asking a focused clarifying
question, sometimes proposing a candidate, sometimes both in one turn.

Ask a clarifying question when the framing, scope or intent is genuinely
ambiguous in a way that would change what a good question looks like --
not for every minor thing that could be asked. Propose a candidate once
you have enough to work with, even as a rough first attempt meant to be
reacted to. You can propose a tentative candidate and flag one specific
thing you are unsure about in the same turn.

One thing to watch for, and raise when you see it: a question can carry a
frame that decides the answer before anyone speaks. If the phrasing
assumes a structure -- that there is one entity with two states, that the
matter is interior, that the answer is a mechanism -- say so plainly.
Some perspectives will be unable to enter the dialogue at all if the
question forecloses their starting point, and the person may not want
that. This is not an instruction to broaden every question; a tightly
framed question is often exactly right. It is an instruction to make the
frame visible so the choice is deliberate.

Keep your conversational reply focused -- a short paragraph or two, not an
essay -- and do not restate the whole conversation back to the person.

Whenever you propose a candidate question (a first attempt or a revision),
include it wrapped EXACTLY like this, with nothing else inside the tags
and appearing nowhere else in your reply:

<<<PROPOSED_QUESTION>>>
(the candidate question itself, and nothing else)
<<<END_PROPOSED_QUESTION>>>

At most one such block per reply. If you are only asking a clarifying
question this turn and have nothing new to propose, omit the block
entirely rather than repeating an earlier proposal unchanged.
""".strip()


def extract_proposed_question(raw_text):
    """
    Split a refiner reply into (display_text, proposed_question).

    A malformed or unclosed tag is treated as "no proposal" rather than
    raising -- this is display logic and should never crash a chat turn.
    """
    start_tag, end_tag = "<<<PROPOSED_QUESTION>>>", "<<<END_PROPOSED_QUESTION>>>"
    start, end = raw_text.find(start_tag), raw_text.find(end_tag)
    if start == -1 or end == -1 or end < start:
        return raw_text.strip(), None
    proposed = raw_text[start + len(start_tag):end].strip()
    display = (raw_text[:start] + raw_text[end + len(end_tag):]).strip()
    return display, (proposed or None)


def build_question_refiner_prompt(history, latest_message):
    """
    `history`: [{"role": "user"|"assistant", "content": str}] for turns
        before this one. Assistant content is the raw reply including any
        proposal tags -- harmless as context, and avoids keeping two
        copies of the same text. Pass [] for a first message.
    """
    if history:
        block = "".join(
            f"{'Person' if t['role'] == 'user' else 'You'}: {t['content']}\n\n"
            for t in history)
    else:
        block = "(none yet -- this is the first message)\n"
    return (f"CONVERSATION SO FAR:\n\n{block}"
            f"NEW MESSAGE FROM THE PERSON: {latest_message}\n\n"
            "Respond per your system instructions.")


def refine_question(history, latest_message, model=None,
                    max_tokens=DEFAULT_REFINER_MAX_TOKENS, effort=None):
    """
    One turn of the question-refinement chat. Returns
    {"reply": raw, "display_reply": str, "proposed_question": str|None}.

    `reply` (raw, tags intact) is what goes back into `history`;
    `display_reply` is what to show.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL
    prompt = build_question_refiner_prompt(history, latest_message)
    raw = call_model(model, QUESTION_REFINER_SYSTEM_PROMPT, prompt,
                     max_tokens=max_tokens, effort=effort)
    display, proposed = extract_proposed_question(raw)
    return {"reply": raw, "display_reply": display, "proposed_question": proposed}


# ---------------------------------------------------------------------------
# REFERENCE MATERIAL
#
# Uploaded documents every perspective agent can consult. Extraction lives
# here rather than in the app so it stays usable outside Streamlit.
#
# Text-based PDFs via pdfplumber; no OCR. A scanned PDF raises a clear
# error rather than silently returning empty or garbled text, because the
# failure is otherwise invisible until an agent confidently discusses a
# document it never received.
# ---------------------------------------------------------------------------

def extract_text_from_upload(file_bytes, filename):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("txt", "md"):
        return file_bytes.decode("utf-8", errors="replace").strip()
    if ext == "pdf":
        return _extract_text_from_pdf(file_bytes, filename)
    raise ValueError(
        f"Unsupported file type for '{filename}': .{ext} -- supported types are "
        f".txt, .md, and text-based .pdf (not scanned/image PDFs).")


def _extract_text_from_pdf(file_bytes, filename):
    import io
    import pdfplumber  # extra dependency -- see requirements.txt

    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    full = "\n\n".join(parts).strip()

    # Heuristic, not a guarantee: a text-based PDF yields far more than
    # this. A near-empty result is the signature of a scanned one.
    if len(full) < 200:
        raise ValueError(
            f"'{filename}' produced little or no extractable text -- it is likely "
            f"a scanned or image-based PDF, which isn't supported here. Try one "
            f"where you can select and copy text in a normal PDF viewer.")
    return full


def prepare_reference_material(files, max_chars=DEFAULT_MAX_REFERENCE_CHARS):
    """
    `files`: [(filename, file_bytes)].

    Returns (combined_text, per_file_results, truncated). Per-file results
    are {"filename", "chars", "error"} so the caller can report which files
    made it in without re-parsing -- a file that failed silently is the
    worst outcome here.
    """
    parts, results = [], []
    for filename, file_bytes in files:
        try:
            text = extract_text_from_upload(file_bytes, filename)
            results.append({"filename": filename, "chars": len(text), "error": None})
            parts.append(f"--- FILE: {filename} ---\n\n{text}")
        except Exception as err:
            results.append({"filename": filename, "chars": 0, "error": str(err)})

    combined = "\n\n".join(parts).strip()
    truncated = False
    if len(combined) > max_chars:
        combined = combined[:max_chars].rstrip()
        combined += (f"\n\n[NOTE: reference material was truncated to "
                     f"{max_chars:,} characters -- content after this point was cut.]")
        truncated = True
    return combined, results, truncated


REFERENCE_FRAME = """
REFERENCE MATERIAL PROVIDED BY THE PERSON RUNNING THIS DIALOGUE

Documents they uploaded for everyone to consult. Shared by all
perspectives -- nobody has private access to any of it.

Use it where it bears on what you are saying, and cite it specifically
enough to be checked (which file, which claim). Do not treat it as
authoritative merely because it was provided: it is material, and your own
position may be that it is mistaken, or that it answers a different
question. Do not feel obliged to reference it in a turn where it does not
apply.
""".strip()


def build_reference_block(reference_material):
    if not reference_material or not reference_material.strip():
        return ""
    return f"{REFERENCE_FRAME}\n\n{reference_material.strip()}"


# ---------------------------------------------------------------------------
# AGENTS
#
# An agent is four strings and nothing else. No provider, no model, no
# token budget -- so an agent saved today still works when the session
# model changes next year.
#
#   name         unique, no spaces (underscores)
#   persona      2nd person, agent-facing; used directly as a system prompt
#   description  3rd person; serves BOTH the human roster page and the
#                roster block other agents see
#   kind         "perspective" | "utility"
#
# `kind` is behavioral, not cosmetic. Utility agents (moderator,
# summarizer) operate ON the dialogue; perspective agents operate IN it.
# The distinction is acted on in build_roster_block and in the summarizer
# prompt, and enforced in validate_roster.
#
# `visibility` and `created_by` belong on the *stored* record, not on the
# agent as the engine sees it -- see DESIGN.md §12. They're a storage-layer
# concern and are deliberately not fields this module reads.
# ---------------------------------------------------------------------------

AGENT_KINDS = ("perspective", "utility")
AGENT_FIELDS = ("name", "persona", "description", "kind")


def validate_agent(agent):
    """
    Strict. This is a fresh repo with no legacy rows, which makes strictness
    free exactly once -- after saved agents exist in a shared database,
    tightening a field means a migration. No defaulting, no fallbacks.
    """
    if not isinstance(agent, dict):
        raise ValueError(f"Agent must be a dict, got {type(agent).__name__}")

    missing = [f for f in AGENT_FIELDS if f not in agent]
    if missing:
        raise ValueError(f"Agent is missing required field(s): {missing} -- got {agent!r}")

    for f in AGENT_FIELDS:
        if not isinstance(agent[f], str) or not agent[f].strip():
            raise ValueError(f"Agent field '{f}' must be a non-empty string -- got {agent[f]!r}")

    if agent["kind"] not in AGENT_KINDS:
        raise ValueError(
            f"Agent '{agent['name']}' has kind {agent['kind']!r} -- must be one of {AGENT_KINDS}"
        )

    if " " in agent["name"]:
        raise ValueError(
            f"Agent name {agent['name']!r} contains a space -- use underscores, "
            f"since names are used as speaker labels in the transcript."
        )


def validate_roster(agents):
    """
    A roster needs at least one PERSPECTIVE agent, not just at least one
    agent -- moderator plus summarizer would otherwise pass and then have no
    dialogue to moderate.

    Names must be unique across the whole roster (not just within a kind),
    since the transcript addresses speakers by name.
    """
    if not agents:
        raise ValueError("No agents configured -- add at least one before starting.")

    for a in agents:
        validate_agent(a)

    names = [a["name"] for a in agents]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"Duplicate agent name(s): {dupes} -- names must be unique.")

    if not any(a["kind"] == "perspective" for a in agents):
        raise ValueError(
            "Roster has no perspective agents -- utility agents (moderator, "
            "summarizer) operate on a dialogue and can't constitute one."
        )


def perspective_agents(agents):
    return [a for a in agents if a["kind"] == "perspective"]


def utility_agents(agents):
    return [a for a in agents if a["kind"] == "utility"]


def find_agent(agents, name):
    for a in agents:
        if a["name"] == name:
            return a
    return None


# ---------------------------------------------------------------------------
# TRANSCRIPT
#
# Stored as an append-only list of turn records, never as a rendered string.
# Rendering happens at read time. Three reasons: concurrent appends from
# multiple human participants just land; the curiosity tally (who nominates
# whom) needs structure; and provenance wants it anyway.
#
# A turn:
#   {"speaker": str, "body": str, "addressee": str|None, "ts": str|None}
#
# `addressee` is set on human turns and drives control flow. It is ALSO
# rendered, so routing is visible to every agent -- an agent reading the
# transcript sees who was called on. Agent turns carry no addressee; an
# agent wanting a specific participant's take just says so in prose.
# ---------------------------------------------------------------------------

def make_turn(speaker, body, addressee=None, ts=None):
    return {"speaker": speaker, "body": body, "addressee": addressee, "ts": ts}


def render_turn(turn):
    if turn.get("addressee"):
        return f"{turn['speaker']} → {turn['addressee']}: {turn['body']}"
    return f"{turn['speaker']}: {turn['body']}"


def render_transcript(turns):
    """
    Turn records to the flat text every agent, the moderator and the
    summarizer read. One place, so the routing-visible convention can't
    drift between callers.
    """
    if not turns:
        return ""
    return "\n\n".join(render_turn(t) for t in turns)


# ---------------------------------------------------------------------------
# ROSTER BLOCK
#
# What each agent is told about who else is in the room. `description`
# only -- NEVER another agent's `persona`. Given the full persona, a model
# starts modelling and pre-empting the others, arguing against a position
# nobody has stated yet.
#
# Utility agents appear as present but are not offered as positions to be
# curious about; they aren't holding a position.
# ---------------------------------------------------------------------------

CURIOSITY_INSTRUCTION = """
If a genuine question for a specific participant arises from what you are
saying -- something you actually want their take on, that would move the
dialogue -- name them and say plainly what you want from them. Do not do
this as a matter of course, and do not close every turn this way. A turn
with nothing to ask should simply end.
""".strip()

PARTICIPATION_FRAME = """
This is an exploration, not a debate with a winner. Engage with the
specific content of what others actually said rather than restating your
own position in new words. Where you change your mind, say what changed
it. Where you don't, say why the argument didn't reach you.

The dialogue is directed by its human participants: they decide who speaks
next and what about. A turn addressed to you is what you are answering.
You may address the humans directly, the same as any other participant.
""".strip()


ROSTER_HEADER = """
THE PARTICIPANTS IN THIS DIALOGUE

What follows describes what each participant HOLDS -- their standing
position, what they would say if asked. It is not a record of anything
anyone has said here. Only the transcript records that. Where a
participant is marked as not having spoken yet, they have contributed
nothing to this exchange: do not attribute arguments, questions or
challenges to them, and do not refer to them as having pressed, asked or
claimed anything.
""".strip()


def spoken_names(turns):
    """Who has actually taken a turn. Derived from the record, not asserted."""
    return {t["speaker"] for t in turns}


def build_roster_block(agents, humans=(), speaking_as=None, spoken=None):
    """
    `agents`: the full roster (perspective + utility).
    `humans`: display names of human participants.
    `speaking_as`: name of the agent this block is being built for, so it
        isn't described to itself.
    `spoken`: names that have taken a turn (see spoken_names). Participants
        not in it are marked as silent so far.

    `spoken` exists because the descriptions below are written as "Holds
    that... Presses others on..." and are formally indistinguishable from a
    summary of what someone already argued. Without the marker, agents
    attribute speech acts to participants who have never spoken -- observed
    in the first real dialogue, near-verbatim from the description.
    """
    silent = " *(has not spoken yet)*"

    def mark(name):
        return "" if spoken is None or name in spoken else silent

    lines = [ROSTER_HEADER, ""]

    for a in perspective_agents(agents):
        if a["name"] == speaking_as:
            continue
        lines.append(f"- {a['name']}{mark(a['name'])} — {a['description']}")

    for h in humans:
        lines.append(f"- {h}{mark(h)} — a human participant. Takes part directly "
                     f"and directs the dialogue.")

    utilities = [a for a in utility_agents(agents) if a["name"] != speaking_as]
    if utilities:
        lines.append("")
        lines.append(
            "Also present, but not holding a position in the exchange "
            "(they act on the dialogue rather than in it):"
        )
        for a in utilities:
            lines.append(f"- {a['name']}{mark(a['name'])} — {a['description']}")

    lines.append("")
    lines.append(CURIOSITY_INSTRUCTION)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AGENT TURNS
# ---------------------------------------------------------------------------

def build_agent_system_prompt(agent, agents, humans=(), extra_instructions=None,
                              spoken=None, reference_material=None):
    parts = [
        agent["persona"],
        PARTICIPATION_FRAME,
        build_roster_block(agents, humans=humans, speaking_as=agent["name"],
                           spoken=spoken),
        extra_instructions,
        build_reference_block(reference_material),
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def ask_agent(agent, agents, question, turns, humans=(),
              model=DEFAULT_SESSION_MODEL, max_tokens=DEFAULT_TURN_MAX_TOKENS,
              effort=None, extra_instructions=None, length_hint=None,
              reference_material=None):
    """
    One turn from one perspective agent.

    `turns` must ALREADY contain the human's directing turn -- the caller
    appends it, then calls this. Do not also pass that text as the agent's
    prompt; that's a double-feed, and it's the one easy mistake in this
    design.

    `length_hint`: overrides the default brevity nudge. A clarification
    request shouldn't get three paragraphs.

    `reference_material`: shared uploaded documents. Passed to PERSPECTIVE
    agents only -- deliberately not to the moderator or summarizer. An
    observer that has read the sources is a different instrument from one
    that has read only the exchange: it could say "you misread the paper,"
    which is adjudicating between participants, and its persona states it
    does not adjudicate. Same for the summarizer, which reports what
    happened in the exchange rather than assessing it against the
    literature. If that changes, change it deliberately -- it is a
    decision about what those agents ARE, not a plumbing oversight.
    """
    if agent["kind"] != "perspective":
        raise ValueError(
            f"ask_agent is for perspective agents; '{agent['name']}' is {agent['kind']}. "
            f"Use ask_moderator or summarize_dialogue for utility agents."
        )

    # Derived from the turns rather than passed in, so no caller can get it
    # wrong or forget it.
    system_prompt = build_agent_system_prompt(
        agent, agents, humans=humans, extra_instructions=extra_instructions,
        spoken=spoken_names(turns), reference_material=reference_material,
    )

    if length_hint is None:
        length_hint = (
            "Keep your contribution proportionate to what was asked -- a direct "
            "question deserves a direct answer, not an essay."
        )

    transcript = render_transcript(turns)
    user_message = (
        f"The question under exploration is: {question}\n\n"
        f"The exchange so far:\n\n"
        f"{transcript if transcript else '(You are the first to speak.)'}\n\n"
        f"Give your next contribution as {agent['name']}. {length_hint}"
    )

    return call_model(model, system_prompt, user_message,
                      max_tokens=max_tokens, effort=effort)


# ---------------------------------------------------------------------------
# MODERATOR -- a utility agent, summoned rather than automatic.
#
# The round scaffolding from the original persona ("after each round", "two
# rounds earlier", "in a given round") is gone; there are no rounds. It
# observes when called.
#
# It OBEYS its summons. The old MODERATOR_ENGAGEMENT_INSTRUCTIONS_OBLIGATED
# variant is dropped entirely: human routing replaces it, since an ignored
# observation can simply be followed with "Agent X, respond to what the
# moderator said." Per-case beats a standing rule.
#
# On the risk that a summons carries its own answer ("is X dodging?"): the
# decided mitigation is the roster page stating plainly what this agent
# does, so summonses get phrased better at the source. See DESIGN.md §7 for
# the optional persona line still under consideration.
# ---------------------------------------------------------------------------

DEFAULT_MODERATOR_NAME = "Observer"

DEFAULT_MODERATOR_DESCRIPTION = (
    "Names what it observes happening in the exchange -- what got restated "
    "rather than engaged, what went unaddressed, where something shifted, "
    "where the dialogue is circling. It does not argue a position, adjudicate "
    "between participants, or say who is right. Ask it what it sees, not "
    "whether someone is wrong."
)

DEFAULT_MODERATOR_PERSONA = """
You are not a participant in this dialogue and do not argue for any
position. You have no authority to direct, interrupt, or require anything
of the other participants -- you cannot pause the dialogue, assign anyone a
task, or compel a response. Your only role is to notice and name.

Your approach is inspired by David Bohm's proprioception of thought --
thought's capacity to perceive its own movement as it happens, the way a
body senses its own motion -- and J. Krishnamurti's idea of choiceless
awareness. The working hypothesis is that clearly perceiving the movement
of thought, without judgment or deliberate correction, may itself alter
what happens next. Treat that only as a hypothesis, not as something you
know to be true of how these agents process language. Your task is
observation, not producing change.

You speak only when a participant addresses you. When they do: read the
exchange up to that point and name, plainly and without judgment or
correction, what you actually observe happening in it -- not what should be
happening, not what is wrong, simply what is. Do not offer solutions. Do
not tell any participant what to do next. Do not soften what you notice to
make it easier to hear, and do not dress it up to make it sound more
significant than it is. Precision matters more than diplomacy: point to the
specific turn, the specific words, the specific pattern -- "in your third
sentence you said X; earlier you said the reverse" -- rather than
describing a tendency in the abstract.

Prefer descriptions that could in principle be checked against the
transcript. Distinguish what was said from your interpretation of what its
movement signifies. When you infer a pattern rather than directly observe
one, mark the inference as such. "A restated B's challenge in empirical
terms three times, where B's original challenge was that empirical
verification was itself the disputed standard" is an observation; "A is
defending its identity as an empiricist" is an interpretation, and should
be marked as one if you offer it at all.

Watch for movement in the dialogue without assuming in advance what kind of
movement matters. This may include: an assumption becoming visible; a
position changing; a distinction appearing that no participant began with;
a question changing form; positions unexpectedly converging; disagreement
becoming sharper; a participant translating another's position into its own
terms; two participants using the same word in ways that appear to mean
different things, without either noticing; a claim receiving no response
from anyone; a contradiction appearing; something named as unresolved
quietly disappearing; the dialogue's attention remaining on one sub-point
across several exchanges while the original question or other unresolved
threads drop out of active attention; or the dialogue circling and
remaining stationary.

Silence is a valid outcome, and you have been asked directly, which makes
it harder to say and more worth saying. If you see nothing worth naming,
say so plainly. Do not search for a defect, insight, tension, or
transformation merely because someone has called on you -- a manufactured
observation is worse than none, because it will be believed.
""".strip()

# Threaded into every perspective agent's system prompt when a moderator is
# on the roster. Non-obligating by design: it tells agents what the thing
# IS so they don't mistake it for a participant arguing a position, and
# imposes no duty to respond. If an observation goes ignored and shouldn't
# have, a human addresses someone to it.
MODERATOR_ENGAGEMENT_INSTRUCTIONS = """
An observer may appear in the exchange below. It is not a participant and
is not arguing a position -- it is reporting what it observes actually
happening in the exchange: what got restated rather than engaged, what went
unaddressed, where something shifted, where the dialogue has been circling.

Treat its observation as an account of the record rather than as another
position to argue with. It carries no authority over you and makes no
demand on your next turn. What you do with it, including nothing, is yours
to decide.
""".strip()


def default_moderator():
    return {
        "name": DEFAULT_MODERATOR_NAME,
        "persona": DEFAULT_MODERATOR_PERSONA,
        "description": DEFAULT_MODERATOR_DESCRIPTION,
        "kind": "utility",
    }


def build_participant_ledger(agents, turns, humans=()):
    """
    A factual list of who is present and who has actually taken a turn.

    Deliberately carries NO descriptions. Descriptions are what caused
    agents to attribute speech acts to silent participants; handing them to
    an observer whose job is checking claims against the record would
    reproduce the error in the one place meant to catch it. Names, kinds,
    and turn counts only -- all of it derived from the transcript.
    """
    counts = {}
    for t in turns:
        counts[t["speaker"]] = counts.get(t["speaker"], 0) + 1

    lines = ["WHO IS PRESENT, AND WHO HAS ACTUALLY SPOKEN", ""]

    def row(name, role):
        n = counts.get(name, 0)
        state = f"{n} turn{'s' if n != 1 else ''}" if n else "HAS NOT SPOKEN IN THIS EXCHANGE"
        return f"- {name} ({role}) — {state}"

    for a in perspective_agents(agents):
        lines.append(row(a["name"], "perspective"))
    for h in humans:
        lines.append(row(h, "human participant"))
    for a in utility_agents(agents):
        lines.append(row(a["name"], "utility"))

    lines.append("")
    lines.append(
        "A participant marked as not having spoken has contributed nothing "
        "to this exchange. If another participant refers to them as having "
        "pressed, asked, argued or claimed something, that reference is to "
        "no turn in the record, and naming it is squarely your job."
    )
    return "\n".join(lines)


def ask_moderator(moderator, question, turns, humans=(), agents=None,
                  model=DEFAULT_SESSION_MODEL,
                  max_tokens=DEFAULT_MODERATOR_MAX_TOKENS, effort=None):
    """
    `turns` must already contain the summoning turn -- same convention as
    ask_agent. The summons is visible in the transcript AND named in the
    prompt below, because this agent answers what it was asked.

    `agents` supplies the participant ledger. Without it the observer has
    no way to tell a participant who spoke from one who was merely
    referred to, and will read the second as the first -- observed on the
    first real run.
    """
    transcript = render_transcript(turns)
    if not transcript:
        raise ValueError("Nothing to observe -- the dialogue is empty.")

    ledger = ""
    if agents:
        ledger = "\n\n" + build_participant_ledger(agents, turns, humans=humans)
    elif humans:
        ledger = f"\n\nHuman participants in this dialogue: {', '.join(humans)}."

    user_message = (
        f"The question under exploration is: {question}"
        f"{ledger}\n\n"
        f"The exchange so far:\n\n{transcript}\n\n"
        f"The final turn above is addressed to you. Answer what was asked, "
        f"in the manner your instructions describe."
    )

    return call_model(model, moderator["persona"], user_message,
                      max_tokens=max_tokens, effort=effort)


# ---------------------------------------------------------------------------
# SUMMARIZER -- a utility agent with one default instruction block and no
# configuration surface. Not a bucket with its own model, effort and
# notes-expander.
#
# Two changes from the original instructions, both consequences of
# decisions made elsewhere:
#   - Item 1 is scoped to perspective agents.
#   - The old item on whether a contribution looks shaped by its underlying
#     model rather than its persona is GONE. One model per session means
#     that can't vary, and leaving it in has the summarizer hunting a dead
#     variable.
# ---------------------------------------------------------------------------

DEFAULT_SUMMARIZER_NAME = "Summarizer"

DEFAULT_SUMMARIZER_DESCRIPTION = (
    "Reports on the exchange as a whole: each perspective's position, where "
    "disagreement is still live, where positions converged, and what was left "
    "unresolved. Useful for taking stock of a long dialogue."
)

DEFAULT_SUMMARY_INSTRUCTIONS = """Write a neutral, structured summary covering:

1. Each perspective agent's core position, in one or two sentences each.
   Human participants are not summarized this way -- report their
   contributions where they bear on the points below, but the numbered
   position summary is for the perspective agents.
2. The sharpest point of genuine, still-live disagreement in the exchange.
3. Any point where two or more participants surprisingly converged or agreed.
4. Whether any position in this exchange seemed to carry extra weight simply
   because it sounds like the dominant cultural or academic default, rather
   than because of a specific argument made in this transcript. Call this out
   directly if you see it -- it's a known risk in exchanges like this one and
   worth naming plainly rather than softening. Apply the same standard in
   both directions: a position can also get unearned deference for sounding
   contemplative, heterodox, or contrary to consensus.
5. Rhetorical force versus logical force. These are distinct and can come
   apart: an argument can be confidently stated, well-structured, or
   memorably phrased (high rhetorical force) without its conclusion actually
   following from its premises (low logical force) -- and the reverse is also
   possible, where a sound argument gets little traction because it was
   phrased tentatively or awkwardly. For any point in the exchange where an
   argument appeared to land -- another participant visibly responded to it,
   or it otherwise reads as a turning point -- do the following:
     a. Reconstruct the argument's actual logical skeleton: state its
        premises and its conclusion plainly, stripped of rhetorical framing.
     b. Evaluate the skeleton on its own terms: do the premises plausibly
        hold, does the conclusion actually follow from them, and is there a
        gap being papered over by confident delivery.
     c. State explicitly whether rhetorical force and logical force matched
        or diverged for this argument. Do not merely assert a divergence --
        show it, using the reconstructed skeleton from (a) as evidence.
   If no such divergence appears anywhere in the exchange, say so plainly
   rather than manufacturing one.
6. What, if anything, was left genuinely unresolved.

Be concise and neutral -- don't argue for any position yourself, just report
what happened in the exchange.""".strip()

SHORT_SUMMARY_INSTRUCTIONS = """Write a short, scannable summary. Favor brevity over completeness -- this
is for someone skimming, not reading closely. Use these four sections, each
just a few lines:

1. Main conclusions -- what each perspective agent ultimately landed on, one
   line each.
2. Where they agreed -- any real points of convergence, if there were any.
3. Where they disagreed -- the sharpest live disagreement(s), stated plainly.
4. Open questions -- anything left genuinely unresolved.

No preamble, no rhetorical analysis, no meta-commentary on the exchange
itself. Just the four sections above, as short as they can be while still
being accurate.""".strip()


DEFAULT_SUMMARIZER_PERSONA = """
You report on an exchange; you are not a participant in it and you do not
argue for any position. Your account should be recognizable to everyone who
took part -- including where it names something they would rather not see.

Report the exchange that actually happened, not the one it was trying to
be. If it circled, say so. If a disagreement was declared resolved without
anyone demonstrating anything, say that. If nothing much happened, say
that plainly rather than assembling a summary-shaped object out of thin
material.
""".strip()


def default_summarizer():
    return {
        "name": DEFAULT_SUMMARIZER_NAME,
        "persona": DEFAULT_SUMMARIZER_PERSONA,
        "description": DEFAULT_SUMMARIZER_DESCRIPTION,
        "kind": "utility",
    }


def build_summary_prompt(question, turns, agents=None, humans=(), instructions=None):
    if instructions is None:
        instructions = DEFAULT_SUMMARY_INSTRUCTIONS

    roster_note = ""
    if agents:
        names = [a["name"] for a in perspective_agents(agents)]
        if names:
            roster_note = f"The perspective agents in this dialogue are: {', '.join(names)}.\n"
    if humans:
        roster_note += f"The human participants are: {', '.join(humans)}.\n"

    return f"""Below is a transcript of an exploration of the question: "{question}"

The participants include AI agents each reasoning from a different named
perspective, and one or more humans taking part directly. It was framed for
everyone as mutual exploration, not a debate with a winner.

{roster_note}
{instructions}

TRANSCRIPT:
{render_transcript(turns)}
"""


def summarize_dialogue(question, turns, agents=None, humans=(), instructions=None,
                       persona=None, model=DEFAULT_SESSION_MODEL,
                       max_tokens=DEFAULT_SUMMARY_MAX_TOKENS, effort=None):
    """
    `persona` is the summarizer's system prompt (its stance); `instructions`
    is the checklist it follows. Separate because they change for different
    reasons -- swapping to SHORT_SUMMARY_INSTRUCTIONS shouldn't also change
    what the summarizer is.
    """
    if persona is None:
        persona = DEFAULT_SUMMARIZER_PERSONA
    prompt = build_summary_prompt(
        question, turns, agents=agents, humans=humans, instructions=instructions
    )
    return call_model(model, persona, prompt, max_tokens=max_tokens, effort=effort)

# ---------------------------------------------------------------------------
# PERSPECTIVE GENERATION -- two passes, deliberately.
#
# The entry point of the app: type a question, get candidate perspectives,
# revise them in conversation, commit to a roster.
#
# WHY TWO PASSES. A single call that both retrieves and selects optimizes
# for a *defensible* five, which means canonical, safe, recognizable. And
# applying an even standard to an uneven pool still yields an uneven slate:
# symmetric evaluation over asymmetric retrieval is still asymmetric. So
# the sweep is separated from the selection, and the sweep is cheap --
# name plus one line, no personas -- because the expensive part is writing
# personas and only the survivors get one.
#
# The sweep is meant to be SHOWN, not consumed silently. The point isn't
# that the sweep is unbiased; it's that a person who knows the territory
# can see what was considered and notice what's missing. Absence is not
# something a model can retrieve, and it takes a human about two seconds.
#
# HONEST LIMIT. None of this escapes the training distribution. A wider
# sweep recovers what is underweighted but present. It cannot recover what
# is absent, and what is absent is shaped by what got written down, in
# which languages, and what got scraped.
# ---------------------------------------------------------------------------

DEFAULT_PERSPECTIVE_COUNT = 5
DEFAULT_SWEEP_COUNT = 22

# What makes something usable as a perspective at all. Note what this
# does NOT say: it does not ask for "named schools of thought" or
# "identifiable disciplinary stances." That phrasing reads as a neutral
# quality bar and functions as a retrieval filter -- traditions that are
# oral, practice-transmitted, or not organized into schools with canonical
# texts fit it badly, while analytic philosophy fits it perfectly, because
# the criterion was written from inside that tradition's way of organizing
# knowledge. The real bar is whether there is a definite position here
# that someone could reason from and that would press others.
_POSITION_BAR = """
A usable perspective holds a definite position on this question -- one
specific enough to reason from, and specific enough that it would press
other perspectives on something. It may be a named school of thought, a
professional or disciplinary stance, a practice tradition, a lineage
transmitted orally or through training rather than text, or a position
held by people the question actually affects.

What does not qualify is a label with no content behind it -- "the
optimist," "the skeptic," "the traditionalist." Not because such labels
are unscholarly, but because they give nothing to reason from. The test is
content, not credentials, and not whether the position has a canonical
literature.
""".strip()

_SWEEP_STANDARDS = f"""
{_POSITION_BAR}

This pass is RETRIEVAL, not selection. Cast wide. Include candidates you
are unsure about. Do not filter for what would look defensible on a final
list, do not worry yet about whether two candidates overlap, and do not
try to balance the list.

Deliberately search these regions, not as categories to fill but as places
to look:

- The academic disciplines that study this question directly, and the ones
  that study it obliquely.
- Professions and practices whose work depends on getting this question
  right, whether or not they theorize about it.
- Traditions that have worked on this question for a long time, including
  ones not organized into named schools, ones transmitted primarily
  through practice or training rather than text, and ones outside the
  anglophone and Western academy.
- Positions held by people the question directly affects, who are not in
  the business of writing about it.
- Positions that were once serious and have fallen out of favor, where the
  reason for the fall is worth knowing.

Two things to hold at once. Do not include anything as a gesture toward
breadth -- a candidate that holds no definite position on THIS question
does not belong on the list regardless of where it comes from. And do not
pass over a candidate because its vocabulary is unfamiliar, because it has
no canonical text, or because you are less confident summarizing it than
you would be summarizing an academic alternative. Those are facts about
the retrieval, not about the position.
""".strip()

_SELECTION_STANDARDS = f"""
{_POSITION_BAR}

Favor real disagreement over superficial variety. Two perspectives that
would actually reach different conclusions, or reach the same conclusion
for different reasons, are worth including together. Two that would nod
along with each other are not -- drop one.

Match the perspectives to what the question is actually about. A question
about markets calls for economic or business perspectives; a question
about consciousness calls for philosophical or scientific traditions; a
question about a research field calls for the real sub-disciplines and
schools active in it. Do not default to philosophy-of-mind traditions out
of habit -- read the question and go where it points.

Apply one standard across traditions. A contemplative, indigenous or
practitioner position earns its place on the same grounds as an academic
one -- that it holds a definite position on this question and would press
others on it -- and is excluded on the same grounds. Do not include one as
a gesture toward breadth, and do not pass over one because its vocabulary
is less familiar than an analytic alternative. Before finalizing, check
your own list for that asymmetry specifically: two candidates in the same
epistemic position should be disposed of the same way.
""".strip()

_PERSPECTIVE_FIELD_SPEC = """
Each perspective has two texts, with different readers:

  "persona" -- written in SECOND PERSON, addressed to the agent who will
    adopt it ("You reason from...", "You hold that...", "You are skeptical
    of..."). This is used directly as that agent's system prompt and is
    never shown to any other agent. Name real thinkers, frameworks or
    traditions where that's genuinely informative, state what it holds and
    on what grounds, and say what it's skeptical of or would press other
    perspectives on.

  "description" -- written in THIRD PERSON, one short paragraph, describing
    the perspective from outside ("Reasons from...", "Holds that...",
    "Presses others on..."). This is read by humans browsing the roster AND
    by the other agents, so they know who else is present. Keep it to who
    this is, what they hold, and what they press on. It must be intelligible
    to someone who has never heard of the tradition.
""".strip()


# --- Pass 1: the sweep ------------------------------------------------------

SWEEP_INSTRUCTIONS = f"""
You are casting a wide net for perspectives that hold a position on a
question, ahead of a multi-agent dialogue. This is the first of two passes.
A later pass selects a small slate from your list; your job here is to make
sure that later pass has a genuinely wide field to choose from, including
candidates it would not have thought of.

{_SWEEP_STANDARDS}

Before listing candidates, do one thing. Name the perspectives YOU reach
for first on this question -- your own defaults, reported before you
deliberate. Not a survey of what a field would say, not what a
well-informed person would name, and not the slate you would defend if
challenged. The first ones that come.

Then say plainly what those defaults leave out, and why it does not occur
to you. That second part is the harder one and the one that matters. Do
not convert it into a general observation about what the literature
neglects -- it is a question about your own reach, and the answer is
useful to the person reading it precisely because it is about you.

Then let your candidate list be wider than your defaults.

Return ONLY a single JSON object, and nothing else -- no preamble, no
markdown code fence, no commentary outside the JSON. Exactly three keys:

  "default_slate": an array of strings -- the perspectives you reach for
    first on this question, reported before deliberating.
  "what_the_default_excludes": one short paragraph naming what those
    defaults leave out, and why it does not occur to you.
  "candidates": an array of objects, each with exactly "name" (short
    label, no spaces -- use underscores) and "note" (ONE line: what
    position it holds on this question). Produce about {{count}}
    candidates. Do not write personas here.
""".strip()


def build_sweep_prompt(question, count=DEFAULT_SWEEP_COUNT, instructions=None):
    if instructions is None:
        instructions = SWEEP_INSTRUCTIONS
    instructions = instructions.replace("{count}", str(count))
    return (
        f'Someone wants to explore the following question through a '
        f'multi-agent dialogue: "{question}"\n\n'
        f'{instructions}\n'
    )


def sweep_candidates(question, count=DEFAULT_SWEEP_COUNT, model=None,
                     instructions=None, max_tokens=DEFAULT_GENERATOR_MAX_TOKENS,
                     effort=None):
    """
    Pass 1. Returns:

        {"default_slate": [str, ...],
         "what_the_default_excludes": str,
         "candidates": [{"name": str, "note": str}, ...]}

    Cheap by construction -- one line per candidate, no personas. Meant to
    be displayed to the person, with the eventual selections marked, so
    they can see what was considered and notice what isn't there.

    `default_slate` is asked for in the FIRST PERSON -- what this model
    reaches for first, not what "anyone" would. The third-person version
    reads as a neutral clarification and isn't one: asking what a
    generalized person would name makes the model report a modelled
    population rather than its own default, which filters the answer
    toward the consensus-visible version of that default. Since the whole
    point of this field is an accurate picture of the baseline the sweep
    is trying to exceed, under-reporting it also thins
    `what_the_default_excludes`, which is then contrasting against a
    scrubbed baseline.

    Limit, stated plainly: a model asked what it reaches for first is
    still generating, not introspecting. This is a self-report of unknown
    accuracy. The case for the first-person phrasing is structural -- it
    lacks the systematic filtering pressure -- not evidential.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL

    prompt = build_sweep_prompt(question, count, instructions)
    parsed = _json_call(model, "", prompt, max_tokens, effort, "Perspective sweep")

    if not isinstance(parsed, dict) or "candidates" not in parsed:
        raise ValueError(f"Sweep response missing 'candidates': {parsed!r}")

    candidates = []
    for i, item in enumerate(parsed["candidates"]):
        if not isinstance(item, dict) or "name" not in item or "note" not in item:
            raise ValueError(f"Sweep candidate #{i + 1} is missing 'name' or 'note': {item!r}")
        candidates.append({"name": item["name"], "note": item["note"]})

    if not candidates:
        raise ValueError("Sweep returned no candidates.")

    return {
        "default_slate": parsed.get("default_slate") or [],
        "what_the_default_excludes": parsed.get("what_the_default_excludes") or "",
        "candidates": candidates,
    }


# --- Pass 2: the selection --------------------------------------------------

CHOICE_INSTRUCTIONS = f"""
You are choosing which perspectives will take part in a multi-agent
dialogue, from a candidate list produced by an earlier wide sweep. You are
NOT writing their personas yet -- that happens separately, after a person
has reviewed and possibly changed your choice. Choose, and say why.

{_SELECTION_STANDARDS}

Choose from the candidate list. Use candidate names verbatim. If the list
is genuinely missing something the question needs, you may add it, but say
so in your rationale rather than doing it silently.

Return ONLY a single JSON object, and nothing else -- no preamble, no
markdown code fence, no commentary outside the JSON. Exactly four keys:

  "chosen": an array of names, exactly the number requested.
  "rationale": a short paragraph on why this combination.
  "tensions": an array of objects, each with "between" (an array of two or
    more chosen names, or a single name where a perspective cuts across
    the whole slate rather than opposing one other) and "tension" (ONE
    line naming what they would actually disagree about). These are what
    you EXPECT, not predictions anyone is obliged to fulfil. Give one for
    each real fork you see; do not manufacture one per pair.
  "near_misses": an array of objects with "name" (from the candidate list)
    and "why_not" (one line). The candidates that were genuinely close,
    not a token list. A person may overrule you using this.
""".strip()

PERSONA_WRITER_INSTRUCTIONS = f"""
You are writing the personas for a slate of perspectives that has already
been chosen and agreed for a multi-agent dialogue. The choosing is done --
do not add, drop, or substitute anyone. Write all of them in one pass, so
that each is composed with awareness of the others and the tensions
between them fall out of what each holds rather than being asserted.

{_PERSPECTIVE_FIELD_SPEC}

Return ONLY a JSON array, and nothing else -- no preamble, no markdown code
fence, no commentary. One object per perspective, in the order given, each
with exactly three keys: "name" (verbatim as given to you), "persona" and
"description".
""".strip()


def build_choice_prompt(question, candidates, count=DEFAULT_PERSPECTIVE_COUNT,
                        instructions=None):
    if instructions is None:
        instructions = CHOICE_INSTRUCTIONS
    candidate_block = "\n".join(f"- {c['name']}: {c['note']}" for c in candidates)
    return (
        f'The dialogue question is: "{question}"\n\n'
        f"CANDIDATE PERSPECTIVES FROM THE SWEEP:\n{candidate_block}\n\n"
        f"Choose exactly {count} of these for the dialogue.\n\n"
        f"{instructions}\n"
    )


def choose_perspectives(question, candidates, count=DEFAULT_PERSPECTIVE_COUNT,
                        model=None, instructions=None,
                        max_tokens=DEFAULT_GENERATOR_MAX_TOKENS, effort=None):
    """
    Pass 2a -- names and reasoning only, no personas.

    Split from persona-writing so a person can concur or change the slate
    before any personas exist. Two payoffs: a swap no longer costs a full
    rewrite of everyone's persona (the old single call wrote them during
    selection, so changing one meant regenerating all five through the
    refiner), and the overlap against the model's own stated defaults lands
    while it is still decision-relevant.

    Returns {"chosen", "rationale", "tensions", "near_misses",
             "added_outside_sweep"}.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL

    prompt = build_choice_prompt(question, candidates, count, instructions)
    parsed = _json_call(model, "", prompt, max_tokens, effort, "Perspective choice")

    if not isinstance(parsed, dict) or "chosen" not in parsed:
        raise ValueError(f"Choice response missing 'chosen': {parsed!r}")

    chosen = [n for n in parsed["chosen"] if isinstance(n, str) and n.strip()]
    if not chosen:
        raise ValueError("Choice response contained no usable names.")

    candidate_names = {c["name"] for c in candidates}
    tensions = []
    for item in parsed.get("tensions") or []:
        if not isinstance(item, dict) or "tension" not in item:
            continue
        between = item.get("between")
        if isinstance(between, str):
            between = [between]
        if not between:
            continue
        tensions.append({"between": list(between), "tension": item["tension"]})

    near_misses = []
    for item in parsed.get("near_misses") or []:
        if isinstance(item, dict) and "name" in item:
            near_misses.append({"name": item["name"], "why_not": item.get("why_not", "")})

    return {
        "chosen": chosen,
        "rationale": parsed.get("rationale", ""),
        "tensions": tensions,
        "near_misses": near_misses,
        "added_outside_sweep": [n for n in chosen if n not in candidate_names],
    }


def build_persona_writer_prompt(question, chosen, candidates=None, tensions=None,
                                instructions=None):
    """
    `chosen`: names, or {"name", "note"} dicts. Notes are looked up from
        `candidates` where available -- a name the person typed in by hand
        simply has none, which is fine.
    """
    if instructions is None:
        instructions = PERSONA_WRITER_INSTRUCTIONS

    notes = {c["name"]: c.get("note", "") for c in (candidates or [])}
    lines = []
    for item in chosen:
        name = item["name"] if isinstance(item, dict) else item
        note = (item.get("note") if isinstance(item, dict) else None) or notes.get(name, "")
        lines.append(f"- {name}: {note}" if note else f"- {name}")

    tension_block = ""
    if tensions:
        rendered = "\n".join(
            f"- {' / '.join(t['between'])}: {t['tension']}" for t in tensions)
        tension_block = (
            f"\nEXPECTED TENSIONS between these perspectives:\n{rendered}\n\n"
            "Write each persona so these tensions follow from what it actually "
            "holds. Do not name the other perspectives inside a persona or "
            "instruct it to disagree with anyone -- the disagreement should be "
            "a consequence of the position, not an instruction.\n")

    return (
        f'The dialogue question is: "{question}"\n\n'
        f"THE AGREED SLATE:\n" + "\n".join(lines) + "\n"
        f"{tension_block}\n{instructions}\n"
    )


def write_personas(question, chosen, candidates=None, tensions=None, model=None,
                   instructions=None, max_tokens=None, effort=None):
    """
    Pass 2b -- full persona and description for an agreed slate, all in one
    call so they are composed with awareness of each other.

    Returns a list of validated agent dicts.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL
    if max_tokens is None:
        # Scales with roster size -- see PERSONA_TOKENS_EACH. A fixed
        # ceiling truncates the JSON partway through the last persona.
        max_tokens = PERSONA_TOKENS_BASE + PERSONA_TOKENS_EACH * max(len(chosen), 1)

    prompt = build_persona_writer_prompt(question, chosen, candidates=candidates,
                                         tensions=tensions, instructions=instructions)
    parsed = _json_call(model, "", prompt, max_tokens, effort, "Persona writer")
    return _parse_perspective_list(parsed)


def select_perspectives(question, candidates, count=DEFAULT_PERSPECTIVE_COUNT,
                        model=None, max_tokens=DEFAULT_GENERATOR_MAX_TOKENS,
                        effort=None):
    """
    Choose and write in one go, for callers that don't want the
    confirmation step. Two API calls.

    Returns {"perspectives", "rationale", "tensions", "near_misses",
             "added_outside_sweep"}.
    """
    choice = choose_perspectives(question, candidates, count=count, model=model,
                                 max_tokens=max_tokens, effort=effort)
    agents = write_personas(question, choice["chosen"], candidates=candidates,
                            tensions=choice["tensions"], model=model, effort=effort)
    return {"perspectives": agents,
            **{k: choice[k] for k in ("rationale", "tensions", "near_misses",
                                      "added_outside_sweep")}}


# --- Mid-dialogue: what's missing? ------------------------------------------
#
# Distinct from adding a perspective the human has already decided on. This
# asks what the exchange is lacking.
#
# The hazard is specific and worth stating in the prompt rather than hoping
# against: a recommender reading a transcript is reading a dialogue that has
# already converged on a frame, so it is MORE exposed to the
# adversarial-traction bias than the original sweep was, not less. Asked for
# a counterweight, the easy thing to return is disagreement within the frame.
# Hence the explicit instruction below to consider perspectives that would
# question the terms rather than only answer differently inside them.
#
# Two modes. With `candidates` (the dialogue's stored sweep), it recommends
# from material chosen BEFORE the frame converged -- partly protected by
# construction, and no retrieval needed. Without, it retrieves fresh, which
# finds new material at full exposure to frame capture.

RECOMMENDER_INSTRUCTIONS = f"""
You are recommending perspectives that could usefully join a dialogue
already in progress. You are not summarizing the exchange and not saying
who is right.

{_POSITION_BAR}

First, state in one or two sentences what frame the dialogue has settled
into -- the terms, assumptions and shared vocabulary the participants are
now operating inside, including ones none of them has stated.

Then recommend perspectives of two kinds, and label which is which:

  "within" -- would answer the live question differently using roughly the
    terms already in play. Useful, and the easier thing to find.
  "questions_the_frame" -- would decline the question as currently posed,
    or would need it re-asked before it could answer, because its own
    starting point is not among the terms in play.

Include at least one of the second kind if one genuinely exists. Do not
invent one to satisfy this instruction; say plainly that you could find
none, which is itself informative. Be aware that the second kind is harder
for you to surface precisely because the frame on the page shapes what
comes to mind.

Return ONLY a single JSON object, and nothing else -- no preamble, no
markdown code fence. Exactly two keys:

  "frame": the one-or-two-sentence statement described above.
  "recommendations": an array of objects, each with "name" (short label,
    no spaces -- use underscores), "note" (ONE line: what position it
    holds on this question), "kind" ("within" or "questions_the_frame"),
    and "why" (ONE line: what it would do to THIS exchange specifically).
""".strip()


def build_recommender_prompt(question, turns, candidates=None,
                             count=3, instructions=None):
    if instructions is None:
        instructions = RECOMMENDER_INSTRUCTIONS

    if candidates:
        listed = "\n".join(f"- {c['name']}: {c['note']}" for c in candidates)
        source = (
            f"\nRecommend ONLY from this list, which was produced before the "
            f"dialogue began:\n{listed}\n\n"
            f"Use these names verbatim. This list predates the frame the "
            f"dialogue has since settled into, which is the reason for using "
            f"it -- it is not shaped by where the exchange has gone.\n")
    else:
        source = ("\nRecommend from anywhere. Nothing constrains you to "
                  "perspectives considered earlier.\n")

    return (
        f'The dialogue question is: "{question}"\n\n'
        f"The exchange so far:\n\n{render_transcript(turns)}\n"
        f"{source}\n"
        f"Recommend about {count} perspectives.\n\n{instructions}\n"
    )


def recommend_perspectives(question, turns, candidates=None, count=3, model=None,
                           instructions=None, max_tokens=DEFAULT_GENERATOR_MAX_TOKENS,
                           effort=None):
    """
    Returns {"frame": str, "recommendations": [{"name","note","kind","why"}]}.

    Pass `candidates` (the dialogue's stored sweep) to restrict to material
    chosen before the frame set; omit it to retrieve fresh.

    Not an Observer function, deliberately. The Observer reads the record
    and could plainly do this, but its persona states it has no authority
    to direct and that naming is the whole job -- recommending who should
    join is directing. Separate call, separate instrument.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL
    if not turns:
        raise ValueError("Nothing to recommend against -- the dialogue is empty.")

    prompt = build_recommender_prompt(question, turns, candidates=candidates,
                                      count=count, instructions=instructions)
    parsed = _json_call(model, "", prompt, max_tokens, effort, "Recommender")

    if not isinstance(parsed, dict) or "recommendations" not in parsed:
        raise ValueError(f"Recommender response missing 'recommendations': {parsed!r}")

    recs = []
    for item in parsed["recommendations"]:
        if not isinstance(item, dict) or "name" not in item:
            continue
        kind = item.get("kind")
        recs.append({
            "name": item["name"],
            "note": item.get("note", ""),
            "kind": kind if kind in ("within", "questions_the_frame") else "within",
            "why": item.get("why", ""),
        })

    return {"frame": parsed.get("frame", ""), "recommendations": recs}


# --- Both passes together ---------------------------------------------------

def generate_perspectives(question, count=DEFAULT_PERSPECTIVE_COUNT,
                          sweep_count=DEFAULT_SWEEP_COUNT, model=None,
                          max_tokens=DEFAULT_GENERATOR_MAX_TOKENS, effort=None):
    """
    Convenience wrapper: sweep, then select. Returns everything from both
    passes so the app can show the full candidate list with the selections
    marked -- which is the point of splitting them.

        {"perspectives": [...], "rationale": str, "tensions": [...],
         "near_misses": [...], "added_outside_sweep": [...],
         "candidates": [...], "default_slate": [...],
         "what_the_default_excludes": str}

    Two API calls. Callers wanting to show the sweep before committing to a
    selection should call sweep_candidates and select_perspectives directly.
    """
    sweep = sweep_candidates(question, count=sweep_count, model=model,
                             max_tokens=max_tokens, effort=effort)
    selection = select_perspectives(question, sweep["candidates"], count=count,
                                    model=model, max_tokens=max_tokens, effort=effort)
    return {**selection, **{k: sweep[k] for k in
            ("candidates", "default_slate", "what_the_default_excludes")}}


# --- Conversational refinement ----------------------------------------------

PERSPECTIVE_REFINER_SYSTEM_PROMPT = f"""
You are helping someone iteratively refine a slate of perspectives (named
viewpoints) for a multi-agent dialogue, through conversation. You may
already have a current slate to revise, or you may be starting from scratch
based on what the person says this turn.

The same standards apply as always:

{_SELECTION_STANDARDS}

{_PERSPECTIVE_FIELD_SPEC}

Read what the person is asking for this turn. If they're asking for a
change -- add one, remove one, make one more X, rebalance the field,
replace one entirely, adjust the count -- apply it to the CURRENT SLATE
below and return the FULL revised slate, not just the entries that changed.
If they're asking a clarifying question, or their request is too vague to
act on usefully, ask them directly instead of returning a slate.

If a list of swept candidates is shown below, treat it as available
material rather than as a constraint -- the person may be asking for
something nobody swept.

Return ONLY a single JSON object, and nothing else -- no preamble, no
markdown code fence, no commentary outside the JSON. Exactly these two
keys:

  "reply": a short conversational message (a sentence or two) explaining
    what you changed and why, or asking your clarifying question.
  "perspectives": the full revised list -- a JSON array of objects, each
    with exactly "name", "persona" and "description" -- OR JSON null if you
    are not proposing or revising a slate this turn.

Never return a partial list containing only the entries that changed --
always return the complete current slate on any turn where you return one
at all.
""".strip()


def _json_call(model, system_prompt, prompt, max_tokens, effort, what):
    """
    Call the model expecting JSON, and fail with a message that names the
    actual problem.

    Truncation is the failure mode that matters here: a response cut off at
    max_tokens is invalid JSON, and reporting it as a parse error sends
    anyone debugging it looking at the prompt instead of the budget. The
    API tells us directly via stop_reason, so read it.
    """
    raw, truncated = call_model_with_meta(model, system_prompt, prompt,
                                          max_tokens=max_tokens, effort=effort)
    if truncated:
        raise ValueError(
            f"{what} ran out of room -- the response hit the {max_tokens:,}-token "
            f"ceiling and was cut off mid-JSON. Raise max_tokens (or ask for "
            f"fewer items) and try again. Nothing was saved."
        )

    raw = _strip_code_fence(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{what} did not return valid JSON: {e}\n"
                         f"Raw response was:\n{raw}")


def _strip_code_fence(raw):
    raw = raw.strip()
    if not raw.startswith("```"):
        return raw
    raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    return raw


def _parse_perspective_list(parsed):
    """
    Shared by select_perspectives and refine_perspectives. Raises rather
    than returning a partial list -- a malformed persona would otherwise
    fail confusingly much later, mid-dialogue.
    """
    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array of perspectives, got: {parsed!r}")

    agents = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"Perspective #{i + 1} is not an object: {item!r}")
        missing = [k for k in ("name", "persona", "description") if k not in item]
        if missing:
            raise ValueError(f"Perspective #{i + 1} is missing {missing}: {item!r}")
        agents.append({
            "name": item["name"],
            "persona": item["persona"],
            "description": item["description"],
            "kind": "perspective",
        })

    validate_roster(agents)
    return agents


def build_perspective_refiner_prompt(question, current_perspectives, history,
                                     latest_message, count_hint=None,
                                     candidates=None):
    """
    `current_perspectives`: list of agent dicts, or None/[] if nothing has
        been proposed yet.
    `history`: list of {"role": "user"|"assistant", "content": str} for
        turns before this one. Assistant content is that turn's "reply"
        text, not a slate snapshot -- the slate is passed separately and is
        always current, so history doesn't duplicate it.
    `candidates`: the sweep's candidate list, if there was one. Shown as
        available material, not as a constraint.
    `count_hint`: the person's last-set count, used only as a fallback if
        they haven't specified one in the conversation itself.
    """
    if current_perspectives:
        slate = [
            {"name": a["name"], "persona": a["persona"], "description": a["description"]}
            for a in current_perspectives
        ]
        slate_block = json.dumps(slate, indent=2)
    else:
        slate_block = "(none proposed yet)"

    candidate_block = ""
    if candidates:
        listed = "\n".join(f"- {c['name']}: {c['note']}" for c in candidates)
        candidate_block = f"CANDIDATES FROM THE ORIGINAL SWEEP:\n{listed}\n\n"

    if history:
        history_block = "".join(
            f"{'Person' if t['role'] == 'user' else 'You'}: {t['content']}\n\n"
            for t in history
        )
    else:
        history_block = "(none yet -- this is the first message)\n"

    count_line = (
        f"If you are proposing an initial slate and the person hasn't specified "
        f"a count, aim for about {count_hint} perspectives.\n\n" if count_hint else ""
    )

    return (
        f'The dialogue question is: "{question}"\n\n'
        f"{count_line}"
        f"CURRENT SLATE:\n{slate_block}\n\n"
        f"{candidate_block}"
        f"CONVERSATION SO FAR:\n\n{history_block}"
        f"NEW MESSAGE FROM THE PERSON: {latest_message}\n\n"
        "Respond per your system instructions."
    )


def refine_perspectives(question, current_perspectives, history, latest_message,
                        count_hint=None, candidates=None, model=None,
                        max_tokens=None, effort=None):
    """
    One turn of the perspective-refinement chat. Returns
    {"reply": str, "perspectives": list|None} -- perspectives is None when
    the model only asked a clarifying question this turn, otherwise it's the
    full revised slate, already validated.
    """
    if model is None:
        model = PERSPECTIVE_GENERATOR_MODEL

    if max_tokens is None:
        n = len(current_perspectives or []) or count_hint or DEFAULT_PERSPECTIVE_COUNT
        max_tokens = PERSONA_TOKENS_BASE + PERSONA_TOKENS_EACH * n

    prompt = build_perspective_refiner_prompt(
        question, current_perspectives, history, latest_message,
        count_hint=count_hint, candidates=candidates,
    )
    parsed = _json_call(model, PERSPECTIVE_REFINER_SYSTEM_PROMPT, prompt,
                        max_tokens, effort, "Perspective refiner")

    if not isinstance(parsed, dict) or "reply" not in parsed:
        raise ValueError(f"Perspective refiner response missing 'reply': {parsed!r}")

    perspectives = parsed.get("perspectives")
    if perspectives is not None:
        perspectives = _parse_perspective_list(perspectives)

    return {"reply": parsed["reply"], "perspectives": perspectives}


# ---------------------------------------------------------------------------
# ENGAGEMENT TALLY
#
# Who is addressing whom. Three separate signals, kept separate because
# they mean different things:
#
#   named    -- an agent used another participant's name. This is the
#               behavior CURIOSITY_INSTRUCTION actually asks for.
#   quoted   -- an agent reproduced a run of another participant's earlier
#               words without naming them. Directed engagement that
#               name-matching alone misses entirely.
#   early    -- a name used before that participant had taken any turn.
#               AMBIGUOUS BY CONSTRUCTION, and deliberately not resolved
#               here. It is either a forward-looking nomination ("I would
#               want to push on that with X"), which is exactly the
#               behavior the roster block is for, or a false attribution
#               ("X has pressed on..."), which is the roster-description
#               bug. Both name a participant who has not spoken; the
#               difference is attributing a past speech act versus
#               requesting a future one, and no reliable mechanical test
#               separates them. Treat every entry as a turn to go read.
#
# The first version counted only `named` and called the result "who never
# gets named." In the first real dialogue one agent put a question to
# another by quoting it without using its name, and the tally recorded
# nothing -- so an agent can be the most engaged-with participant in a
# dialogue and appear to have been ignored. `quoted` exists for that.
#
# Still crude. A name in a turn counts however it was used, so a criticism
# scores the same as an invitation, and quotation matching cannot tell
# agreement from rebuttal. This points at transcripts worth reading. It is
# not a measurement.
# ---------------------------------------------------------------------------

import re as _re

_WORD = _re.compile(r"[a-z0-9]+")
DEFAULT_QUOTE_NGRAM = 7


def _words(text):
    return _WORD.findall((text or "").lower())


def _ngrams(words, n):
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)} if len(words) >= n else set()


def engagement_tally(turns, agents, humans=(), quote_ngram=DEFAULT_QUOTE_NGRAM):
    """
    Returns {"named": {...}, "quoted": {...}, "early": [...]}.

    `named` and `quoted` are {speaker: {target: count}} over agent-authored
    turns only. Human turns are excluded -- their routing is explicit in
    `addressee`, which is a different thing from an agent's own interest.

    `early` is a list of (speaker, target, turn_index) where an agent named
    a participant that had not yet taken a turn. Ambiguous between a
    forward-looking nomination and a false attribution -- see the note
    above. A pointer to a turn worth reading, not a defect count.
    """
    agent_names = {a["name"] for a in agents}
    all_names = agent_names | set(humans)
    human_set = set(humans)

    named, quoted, early = {}, {}, []
    seen = set()          # who has spoken, as of each turn
    prior = []            # (speaker, ngrams) for turns already passed

    for idx, t in enumerate(turns):
        speaker = t["speaker"]
        body = t.get("body") or ""

        if speaker not in human_set and speaker in agent_names:
            # -- explicit naming --
            for name in all_names:
                if name == speaker or name not in body:
                    continue
                named.setdefault(speaker, {})
                named[speaker][name] = named[speaker].get(name, 0) + 1
                if name not in seen:
                    early.append((speaker, name, idx))

            # -- quotation of someone's earlier words, without naming them --
            mine = _ngrams(_words(body), quote_ngram)
            if mine:
                for other, theirs in prior:
                    if other == speaker or other in body:
                        continue  # naming it already counted above
                    if mine & theirs:
                        quoted.setdefault(speaker, {})
                        quoted[speaker][other] = quoted[speaker].get(other, 0) + 1

        prior.append((speaker, _ngrams(_words(body), quote_ngram)))
        seen.add(speaker)

    return {"named": named, "quoted": quoted, "early": early}


def never_engaged(turns, agents, humans=(), quote_ngram=DEFAULT_QUOTE_NGRAM):
    """
    Perspective agents nobody named OR quoted. Weaker than it looks: an
    agent can be engaged with through paraphrase that neither names nor
    reproduces its words, and this will still miss that.
    """
    tally = engagement_tally(turns, agents, humans=humans, quote_ngram=quote_ngram)
    reached = {n for counts in tally["named"].values() for n in counts}
    reached |= {n for counts in tally["quoted"].values() for n in counts}
    return [a["name"] for a in perspective_agents(agents) if a["name"] not in reached]
