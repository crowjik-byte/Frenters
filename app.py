"""
Streamlit UI for the human-directed dialogue app.

ACCESS MODEL (DESIGN.md §12) -- a query-param token checked on load:

    ?k=<app-token>       full app: create dialogues, sweep, manage agents
    ?d=<dialogue-uuid>   participant link: read and append turns on that one
                         dialogue, nothing else
    (neither)            landing page

Issue a different app token per person; each maps to a display name, so
nobody logs in but the app still knows who they are. Not real security --
it filters drive-bys, which at family scale is the actual threat model.

STREAMLIT NOTE. Text widgets keyed with `key=` keep their own last value,
so assigning st.session_state[key] after the widget has rendered once
silently does nothing. Every widget whose value the app needs to overwrite
programmatically therefore carries a generation counter in its key
(_gkey), and bumping the counter remounts it fresh. This is the fix for
the "use these instructions doesn't replace the textbox" class of bug.
"""

import os
import streamlit as st

import engine
import storage as store

st.set_page_config(page_title="Dialogue", layout="wide")


# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

def _secret(name, default=None):
    """st.secrets on Cloud, environment locally, in that order."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


@st.cache_resource
def get_store():
    api_key = _secret("ANTHROPIC_API_KEY")
    if api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
    return store.get_storage(dsn=_secret("DATABASE_URL"))


def _gkey(base):
    """Widget key carrying a generation counter -- bump to remount fresh."""
    gen = st.session_state.get(f"_gen_{base}", 0)
    return f"{base}__{gen}"


def _bump(base):
    st.session_state[f"_gen_{base}"] = st.session_state.get(f"_gen_{base}", 0) + 1


def init_state():
    defaults = {
        "question": "",
        "sweep": None,          # result of engine.sweep_candidates
        "slate": None,          # list of agent dicts
        "selection_meta": None,  # rationale / near_misses / added_outside_sweep
        "refine_history": [],
        "error": "",
        "turn_error": "",
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


# ---------------------------------------------------------------------------
# AUTH-ISH
# ---------------------------------------------------------------------------

def _qp(name):
    """
    One query param as a single string, or None.

    st.query_params yields a string in the live runtime and a list under
    AppTest, and `?d=x&d=y` can produce a list either way. Coercing here
    means nothing downstream has to care.
    """
    v = st.query_params.get(name)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return v or None


def resolve_access(db):
    """
    Returns (mode, participant, dialogue_id).
    mode is "app" | "participant" | "landing".
    """
    app_token = _qp("k")
    dialogue_id = _qp("d")

    participant = db.participant_by_token(app_token) if app_token else None

    if app_token and participant:
        return "app", participant, dialogue_id
    if dialogue_id:
        return "participant", participant, dialogue_id
    return "landing", None, None


def bootstrap_owner(db):
    """
    First run has no participants and therefore no way in. If OWNER_TOKEN
    and OWNER_NAME are configured and nobody exists yet, create the owner.
    """
    token = _secret("OWNER_TOKEN")
    name = _secret("OWNER_NAME", "Owner")
    if token and not db.list_participants():
        db.upsert_participant(name, token)


def guest_name():
    """
    A participant link with no app token: ask for a display name once and
    keep it in session. Not identity, just a label for the transcript --
    the same thing an agent gets.
    """
    if st.session_state.get("guest_name"):
        return st.session_state["guest_name"]
    st.info("You've been invited to join this dialogue.")
    name = st.text_input("What should we call you in the transcript?",
                         key="guest_name_input")
    if name and st.button("Join"):
        st.session_state["guest_name"] = name.strip().replace(" ", "_")
        st.rerun()
    return None


# ---------------------------------------------------------------------------
# SHARED RENDERING
# ---------------------------------------------------------------------------

def render_transcript(turns, agents):
    kinds = {a["name"]: a["kind"] for a in agents}
    for t in turns:
        is_agent = t["speaker"] in kinds
        avatar = "🧭" if kinds.get(t["speaker"]) == "utility" else ("💭" if is_agent else "🗣️")
        with st.chat_message("assistant" if is_agent else "user", avatar=avatar):
            header = t["speaker"]
            if t.get("addressee"):
                header += f" → {t['addressee']}"
            st.markdown(f"**{header}**")
            st.markdown(t["body"])


def render_roster(agents, expanded=False):
    perspectives = engine.perspective_agents(agents)
    utilities = engine.utility_agents(agents)
    with st.expander(f"Who's in this dialogue ({len(perspectives)} perspectives)", expanded=expanded):
        for a in perspectives:
            st.markdown(f"**{a['name']}** — {a['description']}")
        if utilities:
            st.caption("Also present, acting on the dialogue rather than in it:")
            for a in utilities:
                st.markdown(f"**{a['name']}** — {a['description']}")


def render_sweep(sweep, slate_names=None):
    """
    The sweep is meant to be SEEN. Showing which candidates were selected,
    and how much the selection overlaps the model's own stated defaults, is
    the point of running two passes instead of one.
    """
    if not sweep:
        return
    slate_names = set(slate_names or [])
    defaults = sweep.get("default_slate") or []

    with st.expander(f"What was considered ({len(sweep['candidates'])} candidates)", expanded=False):
        if defaults:
            overlap = [n for n in defaults if n in slate_names]
            st.markdown("**Its own first instincts on this question**")
            st.markdown(", ".join(f"`{d}`" for d in defaults))
            if slate_names:
                st.caption(
                    f"{len(overlap)} of {len(defaults)} made the final slate. "
                    "If that's all of them, the wide sweep didn't change anything — "
                    "worth noticing."
                )
        if sweep.get("what_the_default_excludes"):
            st.markdown("**What it says those defaults leave out**")
            st.markdown(sweep["what_the_default_excludes"])

        st.markdown("**Candidates**")
        for c in sweep["candidates"]:
            mark = "**✓**" if c["name"] in slate_names else "　"
            st.markdown(f"{mark} `{c['name']}` — {c['note']}")


# ---------------------------------------------------------------------------
# PAGE: NEW DIALOGUE
# ---------------------------------------------------------------------------

def page_new_dialogue(db, participant):
    st.subheader("New dialogue")

    question = st.text_area(
        "The question",
        value=st.session_state["question"],
        key=_gkey("question_input"),
        height=100,
        placeholder="What question do you want to explore?",
    )
    st.session_state["question"] = question

    c1, c2, c3 = st.columns([1, 1, 2])
    sweep_count = c1.number_input("Sweep width", 8, 40, engine.DEFAULT_SWEEP_COUNT)
    slate_count = c2.number_input("Perspectives", 2, 10, engine.DEFAULT_PERSPECTIVE_COUNT)

    if c3.button("Sweep for perspectives", type="primary", disabled=not question.strip()):
        st.session_state["error"] = ""
        with st.spinner("Casting a wide net…"):
            try:
                st.session_state["sweep"] = engine.sweep_candidates(
                    question, count=int(sweep_count))
                st.session_state["slate"] = None
                st.session_state["selection_meta"] = None
                st.session_state["refine_history"] = []
            except Exception as e:  # surfaced, not swallowed
                st.session_state["error"] = str(e)
        st.rerun()

    if st.session_state["error"]:
        st.error(st.session_state["error"])

    sweep = st.session_state["sweep"]
    if not sweep:
        return

    slate = st.session_state["slate"]
    render_sweep(sweep, [a["name"] for a in slate] if slate else None)

    if slate is None:
        if st.button(f"Select {int(slate_count)} from these"):
            with st.spinner("Selecting and writing personas…"):
                try:
                    result = engine.select_perspectives(
                        question, sweep["candidates"], count=int(slate_count))
                    st.session_state["slate"] = result["perspectives"]
                    st.session_state["selection_meta"] = result
                except Exception as e:
                    st.session_state["error"] = str(e)
            st.rerun()
        return

    # --- the slate ---
    st.markdown("### Proposed slate")
    meta = st.session_state["selection_meta"] or {}
    if meta.get("rationale"):
        st.caption(meta["rationale"])
    if meta.get("added_outside_sweep"):
        st.warning("Added by the selector, not in the sweep: "
                   + ", ".join(f"`{n}`" for n in meta["added_outside_sweep"]))

    for a in slate:
        with st.expander(f"**{a['name']}** — {a['description']}", expanded=False):
            st.markdown("*Persona (what this agent is told):*")
            st.code(a["persona"], language=None)

    if meta.get("near_misses"):
        with st.expander("Close calls it rejected"):
            for nm in meta["near_misses"]:
                st.markdown(f"`{nm['name']}` — {nm.get('why_not', '')}")

    # --- refinement chat ---
    st.markdown("### Adjust the slate")
    for turn in st.session_state["refine_history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])

    msg = st.chat_input("Swap one out, add something nobody swept, rebalance…")
    if msg:
        st.session_state["refine_history"].append({"role": "user", "content": msg})
        with st.spinner("Revising…"):
            try:
                result = engine.refine_perspectives(
                    question, slate, st.session_state["refine_history"][:-1], msg,
                    candidates=sweep["candidates"])
                st.session_state["refine_history"].append(
                    {"role": "assistant", "content": result["reply"]})
                if result["perspectives"] is not None:
                    st.session_state["slate"] = result["perspectives"]
            except Exception as e:
                st.session_state["refine_history"].append(
                    {"role": "assistant", "content": f"Error: {e}"})
        st.rerun()

    # --- commit ---
    st.divider()
    title = st.text_input("Title", value=question[:60], key=_gkey("new_title"))
    c1, c2 = st.columns(2)
    add_moderator = c1.checkbox("Include the Observer", value=True)
    save_to_library = c2.checkbox("Save these agents to the library", value=True)

    if st.button("Start dialogue", type="primary"):
        roster = list(slate)
        if add_moderator:
            roster.append(engine.default_moderator())
        roster.append(engine.default_summarizer())
        try:
            engine.validate_roster(roster)
        except ValueError as e:
            st.error(str(e))
            return

        if save_to_library:
            for a in slate:
                db.save_agent(a, participant["id"], source_question=question)

        d = db.create_dialogue(title or question[:60], question, roster,
                               participant["id"], sweep=sweep)
        st.session_state["sweep"] = None
        st.session_state["slate"] = None
        st.session_state["selection_meta"] = None
        st.session_state["refine_history"] = []
        st.query_params["d"] = d["id"]
        st.rerun()


# ---------------------------------------------------------------------------
# PAGE: DIALOGUE
# ---------------------------------------------------------------------------

def page_dialogue(db, dialogue_id, speaker_name, participant_id=None, can_share=False):
    d = db.get_dialogue(dialogue_id)
    if not d:
        st.error("No dialogue at that link.")
        return

    agents = d["agents"]
    turns = db.list_turns(dialogue_id)

    if st.session_state.get("turn_error"):
        st.error(st.session_state.pop("turn_error"))

    st.subheader(d["title"])
    st.caption(d["question"])
    render_roster(agents)

    if can_share:
        with st.expander("Invite someone"):
            st.caption("Anyone with this link can read the dialogue and add turns to it. "
                       "It grants nothing else — no access to your agent library or other dialogues.")
            st.code(f"?d={dialogue_id}", language=None)
            st.caption("Append that to the app's URL.")

    # Humans are whoever has actually spoken, plus whoever is here now.
    agent_names = {a["name"] for a in agents}
    humans = []
    for t in turns:
        if t["speaker"] not in agent_names and t["speaker"] not in humans:
            humans.append(t["speaker"])
    if speaker_name not in humans:
        humans.append(speaker_name)

    st.divider()
    if turns:
        render_transcript(turns, agents)
    else:
        st.info("Nobody has spoken yet. Address someone below to begin.")

    # --- compose ---
    st.divider()
    addressable = [a["name"] for a in agents] + [h for h in humans if h != speaker_name]
    c1, c2 = st.columns([1, 3])
    addressee = c1.selectbox("Speaking to", addressable, key=_gkey("addressee"))
    body = c2.text_area("Your turn", key=_gkey("compose"), height=120,
                        placeholder="Ask, push back, add your own view, redirect…")

    c1, c2, c3 = st.columns([1, 1, 2])

    if c1.button("Send", type="primary", disabled=not body.strip()):
        # Append the human's turn FIRST, then invoke -- never both feed the
        # same text to the agent (see engine.ask_agent).
        db.append_turn(dialogue_id, speaker_name, body.strip(),
                       addressee=addressee, author_id=participant_id)

        target = engine.find_agent(agents, addressee)
        # A turn addressed to another human invokes nothing. This is the
        # append/invoke split that makes multiple participants work.
        if target is not None:
            all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                         for t in db.list_turns(dialogue_id)]
            try:
                if target["kind"] == "utility" and target["name"] == engine.DEFAULT_MODERATOR_NAME:
                    with st.spinner(f"{target['name']} is reading the exchange…"):
                        reply = engine.ask_moderator(target, d["question"], all_turns, humans=humans)
                elif target["kind"] == "utility" and target["name"] == engine.DEFAULT_SUMMARIZER_NAME:
                    with st.spinner("Summarizing…"):
                        reply = engine.summarize_dialogue(d["question"], all_turns,
                                                          agents=agents, humans=humans)
                else:
                    with st.spinner(f"{target['name']} is thinking…"):
                        extra = (engine.MODERATOR_ENGAGEMENT_INSTRUCTIONS
                                 if engine.find_agent(agents, engine.DEFAULT_MODERATOR_NAME)
                                 else None)
                        reply = engine.ask_agent(target, agents, d["question"], all_turns,
                                                 humans=humans, extra_instructions=extra)
                db.append_turn(dialogue_id, target["name"], reply)
            except Exception as e:
                # Must survive the rerun below, or the user sees nothing at
                # all -- st.error() written here is discarded on rerun.
                st.session_state["turn_error"] = f"{target['name']} failed to respond: {e}"

        _bump("compose")
        st.rerun()

    if c2.button("Refresh"):
        st.rerun()

    if c3.button("Ask the Summarizer") and turns:
        all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                     for t in turns]
        with st.spinner("Summarizing…"):
            try:
                summary = engine.summarize_dialogue(d["question"], all_turns,
                                                    agents=agents, humans=humans)
                db.append_turn(dialogue_id, engine.DEFAULT_SUMMARIZER_NAME, summary)
            except Exception as e:
                st.session_state["turn_error"] = f"Summarizer failed: {e}"
        st.rerun()

    # --- who's been named ---
    if len(turns) > 3:
        with st.expander("Who's being asked for"):
            st.caption(
                "Crude — a name match in a turn's body. It over-counts passing mentions "
                "and can't tell a nomination from a criticism. Useful mainly for who "
                "never gets named at all."
            )
            all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                         for t in turns]
            tally = engine.curiosity_tally(all_turns, agents, humans=humans)
            if tally:
                for speaker, counts in tally.items():
                    listed = ", ".join(f"{n} ({c})" for n, c in
                                       sorted(counts.items(), key=lambda kv: -kv[1]))
                    st.markdown(f"**{speaker}** mentions: {listed}")
            else:
                st.markdown("*Nobody has named anybody yet.*")
            never = engine.never_named(all_turns, agents, humans=humans)
            if never:
                st.markdown("**Never named by anyone:** " + ", ".join(never))


# ---------------------------------------------------------------------------
# PAGE: AGENT LIBRARY
# ---------------------------------------------------------------------------

def page_library(db, participant):
    st.subheader("Agent library")
    agents = db.list_agents(created_by=participant["id"])
    if not agents:
        st.info("No saved agents yet. They're saved automatically when you start a dialogue.")
        return

    st.caption(f"{len(agents)} saved.")
    for a in agents:
        label = f"**{a['name']}** — {a['description'][:120]}"
        with st.expander(label):
            st.markdown(f"*{a['description']}*")
            st.code(a["persona"], language=None)
            meta = []
            if a.get("source_question"):
                meta.append(f"From: *{a['source_question']}*")
            meta.append(f"Visibility: `{a.get('visibility', 'personal')}`")
            meta.append(f"Saved: {a['created_at'][:10]}")
            st.caption(" · ".join(meta))

            c1, c2 = st.columns([1, 4])
            if c1.button("Delete", key=f"del_{a['id']}"):
                db.delete_agent(a["id"])
                st.rerun()
            new_vis = "public" if a.get("visibility") == "personal" else "personal"
            if c2.button(f"Make {new_vis}", key=f"vis_{a['id']}"):
                # visibility lives on the stored record; re-save with the flip
                db.delete_agent(a["id"])
                db.save_agent(a, a["created_by"], source_question=a.get("source_question"),
                              visibility=new_vis, agent_id=a["id"])
                st.rerun()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    init_state()
    db = get_store()
    bootstrap_owner(db)
    mode, participant, dialogue_id = resolve_access(db)

    if mode == "landing":
        st.title("Dialogue")
        st.markdown(
            "A tool for thinking through a question with several named perspectives, "
            "one turn at a time. You decide who speaks next and what about."
        )
        st.info("You need a link to get in. Ask whoever sent you here.")
        return

    if mode == "participant":
        # Check the link is live BEFORE asking for a name -- otherwise a dead
        # link makes you introduce yourself and then tells you it's dead.
        if not db.get_dialogue(dialogue_id):
            st.title("Dialogue")
            st.error("No dialogue at that link. It may have been deleted, or the link may be incomplete.")
            return
        name = participant["display_name"] if participant else guest_name()
        if not name:
            return
        page_dialogue(db, dialogue_id, name,
                      participant_id=participant["id"] if participant else None,
                      can_share=False)
        return

    # --- full app ---
    with st.sidebar:
        st.markdown(f"**{participant['display_name']}**")
        dialogues = db.list_dialogues(created_by=participant["id"])
        options = ["＋ New dialogue"] + [d["title"] for d in dialogues] + ["Agent library"]
        current = 0
        if dialogue_id:
            for i, d in enumerate(dialogues):
                if d["id"] == dialogue_id:
                    current = i + 1
        choice = st.radio("Go to", options, index=current, label_visibility="collapsed")

    if choice == "＋ New dialogue":
        if dialogue_id:
            del st.query_params["d"]
        page_new_dialogue(db, participant)
    elif choice == "Agent library":
        page_library(db, participant)
    else:
        chosen = next(d for d in dialogues if d["title"] == choice)
        if _qp("d") != chosen["id"]:
            st.query_params["d"] = chosen["id"]
        page_dialogue(db, chosen["id"], participant["display_name"],
                      participant_id=participant["id"], can_share=True)


if __name__ == "__main__":
    main()
