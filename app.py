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
        "choice": None,         # result of engine.choose_perspectives (names only)
        "slate": None,          # list of agent dicts, once personas are written
        "refine_history": [],
        "q_history": [],        # question-refinement chat
        "reference": None,      # {"text":..., "files":[...]}
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
# SHARED: THE PICKER
#
# One component over three sources -- the dialogue's stored sweep
# candidates, the saved library, and freshly typed names. Used when
# confirming a new slate, adding to a dialogue in progress, and starting
# from the library. Building it once is why those three are cheap.
# ---------------------------------------------------------------------------

def pick_names(key, candidates=None, library=None, preselected=(), allow_free=True,
               label="Who's in"):
    """
    Returns the currently chosen list of names. Pure UI -- no writes, no
    model calls. `candidates` are {"name","note"}; `library` are agent
    records.
    """
    state_key = f"_picked_{key}"
    if state_key not in st.session_state:
        st.session_state[state_key] = list(preselected)
    picked = st.session_state[state_key]

    st.markdown(f"**{label}** — {len(picked)} selected")

    if candidates:
        st.caption("From the sweep")
        for c in candidates:
            name = c["name"]
            on = name in picked
            c1, c2 = st.columns([1, 6])
            if c1.checkbox(" ", value=on, key=f"{key}_c_{name}",
                           label_visibility="collapsed") != on:
                picked.remove(name) if on else picked.append(name)
                st.rerun()
            c2.markdown(f"{'**' + name + '**' if on else name} — {c['note']}")

    if library:
        st.caption("From your library")
        lib_names = [a["name"] for a in library]
        chosen_lib = st.multiselect("Library agents", lib_names,
                                    default=[n for n in picked if n in lib_names],
                                    key=f"{key}_lib", label_visibility="collapsed")
        for n in chosen_lib:
            if n not in picked:
                picked.append(n)
        for n in lib_names:
            if n in picked and n not in chosen_lib:
                picked.remove(n)

    if allow_free:
        extra = st.text_input(
            "Add one nobody swept (name, then a colon, then one line on what it holds)",
            key=_gkey(f"{key}_free"),
            placeholder="Yogacara: alaya-vijnana, bija, vasana — seeds perfuming the store-consciousness")
        if extra.strip() and st.button("Add", key=f"{key}_addfree"):
            nm = extra.split(":", 1)[0].strip().replace(" ", "_")
            note = extra.split(":", 1)[1].strip() if ":" in extra else ""
            if nm and nm not in picked:
                picked.append(nm)
                st.session_state.setdefault(f"_freenotes_{key}", {})[nm] = note
            _bump(f"{key}_free")
            st.rerun()

    st.session_state[state_key] = picked
    return picked


def picked_with_notes(key, picked, candidates=None):
    """Names paired with whatever note is known for them, for the persona writer."""
    notes = {c["name"]: c.get("note", "") for c in (candidates or [])}
    notes.update(st.session_state.get(f"_freenotes_{key}", {}))
    return [{"name": n, "note": notes.get(n, "")} for n in picked]


def render_tensions(selection, compact=False):
    """Expected, not required -- a dialogue that doesn't produce them is a
    finding about the slate, not a failure to cover the material."""
    if not selection or not selection.get("tensions"):
        return
    st.caption("Expected tensions — what the slate was built to produce. "
               "Predictions, not a checklist.")
    for t in selection["tensions"]:
        who = " / ".join(t["between"])
        st.markdown(f"- **{who}** — {t['tension']}")
    if not compact and selection.get("rationale"):
        with st.expander("Why this combination"):
            st.markdown(selection["rationale"])


# ---------------------------------------------------------------------------
# PAGE: NEW DIALOGUE
# ---------------------------------------------------------------------------

def page_new_dialogue(db, participant):
    st.subheader("New dialogue")

    library = db.list_agents(created_by=participant["id"])
    if library:
        with st.expander(f"Or start from your library ({len(library)} saved agents)"):
            st.caption("Skips the sweep. Useful for continuing a question with the "
                       "same slate after a long dialogue has gone stale.")
            lq = st.text_area("The question", key=_gkey("lib_question"), height=80)
            lib_picked = pick_names("libstart", library=library, allow_free=False,
                                    label="Roster")
            if st.button("Start from library", disabled=not (lq.strip() and lib_picked)):
                roster = [{k: a[k] for k in ("name", "persona", "description", "kind")}
                          for a in library if a["name"] in lib_picked]
                roster.append(engine.default_moderator())
                roster.append(engine.default_summarizer())
                try:
                    engine.validate_roster(roster)
                except ValueError as err:
                    st.error(str(err))
                    return
                d = db.create_dialogue(lq[:60], lq, roster, participant["id"])
                st.session_state["_picked_libstart"] = []
                st.query_params["d"] = d["id"]
                st.rerun()

    question = st.text_area("The question", value=st.session_state["question"],
                            key=_gkey("question_input"), height=100,
                            placeholder="What question do you want to explore?")
    st.session_state["question"] = question

    with st.expander("Work on the question first"):
        st.caption("The question conditions the whole sweep, so this is the "
                   "highest-leverage edit in the flow. It will also flag where "
                   "the phrasing carries a frame that decides the answer.")
        for turn in st.session_state["q_history"]:
            with st.chat_message(turn["role"]):
                st.markdown(turn.get("display", turn["content"]))
        qmsg = st.chat_input("What are you actually trying to find out?", key="q_chat")
        if qmsg:
            st.session_state["q_history"].append(
                {"role": "user", "content": qmsg, "display": qmsg})
            with st.spinner("Thinking about the question…"):
                try:
                    hist = [{"role": t["role"], "content": t["content"]}
                            for t in st.session_state["q_history"][:-1]]
                    r = engine.refine_question(hist, qmsg)
                    st.session_state["q_history"].append(
                        {"role": "assistant", "content": r["reply"],
                         "display": r["display_reply"]})
                    if r["proposed_question"]:
                        st.session_state["q_proposal"] = r["proposed_question"]
                except Exception as err:
                    st.session_state["q_history"].append(
                        {"role": "assistant", "content": f"Error: {err}",
                         "display": f"Error: {err}"})
            st.rerun()

        prop = st.session_state.get("q_proposal")
        if prop:
            st.markdown("**Proposed question**")
            st.info(prop)
            if st.button("Use this question"):
                st.session_state["question"] = prop
                st.session_state["q_proposal"] = None
                _bump("question_input")
                st.rerun()

    with st.expander("Reference material (optional)"):
        st.caption("Shared by every perspective — nobody gets private access. "
                   ".txt, .md, and text-based PDFs; no OCR, so a scanned PDF "
                   "will be reported as failed rather than silently dropped.")
        uploads = st.file_uploader("Documents", type=["txt", "md", "pdf"],
                                   accept_multiple_files=True, key=_gkey("refs"))
        if uploads and st.button("Read these"):
            with st.spinner("Extracting…"):
                text, results, truncated = engine.prepare_reference_material(
                    [(f.name, f.getvalue()) for f in uploads])
                st.session_state["reference"] = (
                    {"text": text, "files": results, "truncated": truncated}
                    if text else None)
            st.rerun()

        ref = st.session_state.get("reference")
        if ref:
            for f in ref["files"]:
                if f["error"]:
                    st.error(f"{f['filename']}: {f['error']}")
                else:
                    st.markdown(f"- **{f['filename']}** — {f['chars']:,} characters")
            if ref.get("truncated"):
                st.warning("Combined material was truncated to fit the per-turn budget.")
            if st.button("Clear reference material"):
                st.session_state["reference"] = None
                st.rerun()

    c1, c2, c3 = st.columns([1, 1, 2])
    sweep_count = c1.number_input("Sweep width", 8, 40, engine.DEFAULT_SWEEP_COUNT)
    slate_count = c2.number_input("Perspectives", 2, 10, engine.DEFAULT_PERSPECTIVE_COUNT)

    if c3.button("Sweep for perspectives", type="primary", disabled=not question.strip()):
        st.session_state["error"] = ""
        with st.spinner("Casting a wide net…"):
            try:
                st.session_state["sweep"] = engine.sweep_candidates(question, count=int(sweep_count))
                for k in ("choice", "slate", "refine_history"):
                    st.session_state[k] = None if k != "refine_history" else []
                st.session_state["_picked_slate"] = None
            except Exception as err:
                st.session_state["error"] = str(err)
        st.rerun()

    if st.session_state["error"]:
        st.error(st.session_state["error"])

    sweep = st.session_state["sweep"]
    if not sweep:
        return

    choice = st.session_state.get("choice")
    slate = st.session_state.get("slate")
    render_sweep(sweep, [a["name"] for a in slate] if slate else
                 (choice["chosen"] if choice else None))

    # --- step 1: choose (names only, no personas yet) ---
    if not choice:
        if st.button(f"Choose {int(slate_count)} from these"):
            with st.spinner("Choosing…"):
                try:
                    st.session_state["choice"] = engine.choose_perspectives(
                        question, sweep["candidates"], count=int(slate_count))
                    st.session_state["_picked_slate"] = list(
                        st.session_state["choice"]["chosen"])
                except Exception as err:
                    st.session_state["error"] = str(err)
            st.rerun()
        return

    # --- step 2: confirm or change, before any personas are written ---
    if not slate:
        st.markdown("### Proposed slate")
        if choice.get("rationale"):
            st.caption(choice["rationale"])
        if choice.get("added_outside_sweep"):
            st.warning("Added by the selector, not in the sweep: "
                       + ", ".join(f"`{n}`" for n in choice["added_outside_sweep"]))
        render_tensions(choice, compact=True)

        defaults = sweep.get("default_slate") or []
        if defaults:
            overlap = [n for n in defaults if n in choice["chosen"]]
            st.caption(f"{len(overlap)} of {len(defaults)} of its own first instincts "
                       f"made this slate. All of them would mean the wide sweep "
                       f"changed nothing.")

        if choice.get("near_misses"):
            with st.expander("Close calls it rejected"):
                for nm in choice["near_misses"]:
                    st.markdown(f"`{nm['name']}` — {nm.get('why_not', '')}")

        st.divider()
        picked = pick_names("slate", candidates=sweep["candidates"], library=library,
                            preselected=choice["chosen"], label="Agreed slate")

        c1, c2 = st.columns([1, 3])
        if c1.button("Write personas", type="primary", disabled=len(picked) < 2):
            with st.spinner("Writing personas…"):
                try:
                    st.session_state["slate"] = engine.write_personas(
                        question, picked_with_notes("slate", picked, sweep["candidates"]),
                        candidates=sweep["candidates"], tensions=choice["tensions"])
                except Exception as err:
                    st.session_state["error"] = str(err)
            st.rerun()
        if c2.button("Choose again"):
            st.session_state["choice"] = None
            st.session_state["_picked_slate"] = None
            st.rerun()
        return

    # --- step 3: the written slate ---
    st.markdown("### The slate")
    for a in slate:
        with st.expander(f"**{a['name']}** — {a['description']}"):
            st.markdown("*Persona (what this agent is told):*")
            st.code(a["persona"], language=None)

    st.markdown("#### Adjust")
    for turn in st.session_state["refine_history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])

    msg = st.chat_input("Reword a persona, swap someone, rebalance…")
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
            except Exception as err:
                st.session_state["refine_history"].append(
                    {"role": "assistant", "content": f"Error: {err}"})
        st.rerun()

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
        except ValueError as err:
            st.error(str(err))
            return

        if save_to_library:
            for a in slate:
                db.save_agent(a, participant["id"], source_question=question)

        d = db.create_dialogue(
            title or question[:60], question, roster, participant["id"],
            sweep=sweep,
            selection={k: choice[k] for k in ("rationale", "tensions", "near_misses")},
            reference=st.session_state.get("reference"),
        )
        for k in ("sweep", "choice", "slate", "reference", "q_proposal"):
            st.session_state[k] = None
        st.session_state["refine_history"] = []
        st.session_state["q_history"] = []
        st.session_state["_picked_slate"] = None
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
    if d.get("selection"):
        with st.expander("Expected tensions"):
            render_tensions(d["selection"])

    ref = d.get("reference") or {}
    ok_files = [f for f in ref.get("files", []) if not f.get("error")]
    if ok_files or can_share:
        label = (f"Reference material ({len(ok_files)} file"
                 f"{'s' if len(ok_files) != 1 else ''})" if ok_files
                 else "Reference material (none)")
        with st.expander(label):
            for f in ok_files:
                st.markdown(f"- **{f['filename']}** — {f['chars']:,} characters")
            st.caption("Available to every perspective agent. Not given to the "
                       "Observer or the Summarizer: an observer that has read the "
                       "sources could say someone misread a paper, which is "
                       "adjudicating, and its whole role is not to.")
            if can_share:
                more = st.file_uploader("Add more", type=["txt", "md", "pdf"],
                                        accept_multiple_files=True,
                                        key=_gkey(f"moreref_{dialogue_id}"))
                if more and st.button("Add to this dialogue"):
                    with st.spinner("Extracting…"):
                        try:
                            text, results, trunc = engine.prepare_reference_material(
                                [(f.name, f.getvalue()) for f in more])
                            merged_text = "\n\n".join(
                                x for x in [ref.get("text", ""), text] if x)
                            db.update_dialogue(dialogue_id, reference={
                                "text": merged_text,
                                "files": ref.get("files", []) + results,
                                "truncated": ref.get("truncated") or trunc})
                            _bump(f"moreref_{dialogue_id}")
                        except Exception as err:
                            st.session_state["turn_error"] = f"Could not read files: {err}"
                    st.rerun()

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
                        reply = engine.ask_moderator(target, d["question"], all_turns,
                                                     humans=humans, agents=agents)
                elif target["kind"] == "utility" and target["name"] == engine.DEFAULT_SUMMARIZER_NAME:
                    with st.spinner("Summarizing…"):
                        reply = engine.summarize_dialogue(d["question"], all_turns,
                                                          agents=agents, humans=humans)
                else:
                    with st.spinner(f"{target['name']} is thinking…"):
                        extra = (engine.MODERATOR_ENGAGEMENT_INSTRUCTIONS
                                 if engine.find_agent(agents, engine.DEFAULT_MODERATOR_NAME)
                                 else None)
                        ref = (d.get("reference") or {}).get("text")
                        reply = engine.ask_agent(target, agents, d["question"], all_turns,
                                                 humans=humans, extra_instructions=extra,
                                                 reference_material=ref)
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

    # --- add a perspective mid-dialogue ---
    if can_share:
        with st.expander("Bring in another perspective"):
            sweep = d.get("sweep") or {}
            cands = sweep.get("candidates") or []
            library = db.list_agents(created_by=participant_id) if participant_id else []
            here = {a["name"] for a in agents}
            cands = [c for c in cands if c["name"] not in here]
            library = [a for a in library if a["name"] not in here]

            rec = st.session_state.get(f"_rec_{dialogue_id}")
            c1, c2 = st.columns(2)
            if c1.button("What's missing? (from the original sweep)", disabled=not turns):
                with st.spinner("Reading the exchange…"):
                    try:
                        all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                                     for t in turns]
                        st.session_state[f"_rec_{dialogue_id}"] = engine.recommend_perspectives(
                            d["question"], all_turns, candidates=cands or None)
                    except Exception as err:
                        st.session_state["turn_error"] = f"Recommender failed: {err}"
                st.rerun()
            if c2.button("What's missing? (search fresh)", disabled=not turns):
                with st.spinner("Reading the exchange…"):
                    try:
                        all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                                     for t in turns]
                        st.session_state[f"_rec_{dialogue_id}"] = engine.recommend_perspectives(
                            d["question"], all_turns, candidates=None)
                    except Exception as err:
                        st.session_state["turn_error"] = f"Recommender failed: {err}"
                st.rerun()

            if rec:
                if rec.get("frame"):
                    st.caption("What it reads the dialogue as having settled into:")
                    st.markdown(f"*{rec['frame']}*")
                for r in rec["recommendations"]:
                    tag = ("**questions the frame**" if r["kind"] == "questions_the_frame"
                           else "within the frame")
                    st.markdown(f"- `{r['name']}` ({tag}) — {r['note']}")
                    if r.get("why"):
                        st.caption(f"  {r['why']}")
                rec_cands = [{"name": r["name"], "note": r["note"]}
                             for r in rec["recommendations"] if r["name"] not in here]
                cands = cands + [c for c in rec_cands
                                 if c["name"] not in {x["name"] for x in cands}]

            st.divider()
            picked = pick_names(f"add_{dialogue_id}", candidates=cands, library=library,
                                label="Add to this dialogue")
            if st.button("Write personas and add", disabled=not picked):
                with st.spinner("Writing…"):
                    try:
                        lib_by_name = {a["name"]: a for a in library}
                        fresh = [n for n in picked if n not in lib_by_name]
                        new_agents = [
                            {k: lib_by_name[n][k] for k in ("name", "persona", "description", "kind")}
                            for n in picked if n in lib_by_name]
                        if fresh:
                            new_agents += engine.write_personas(
                                d["question"],
                                picked_with_notes(f"add_{dialogue_id}", fresh, cands),
                                candidates=cands)
                        db.update_dialogue(dialogue_id, agents=agents + new_agents)
                        st.session_state[f"_picked_add_{dialogue_id}"] = []
                        st.session_state[f"_rec_{dialogue_id}"] = None
                    except Exception as err:
                        st.session_state["turn_error"] = f"Could not add: {err}"
                st.rerun()

    # --- who's addressing whom ---
    if len(turns) > 3:
        with st.expander("Who's addressing whom"):
            st.caption(
                "Crude, and a pointer to turns worth reading rather than a measurement. "
                "A name counts however it was used, so a criticism scores the same as an "
                "invitation; quotation matching can't tell agreement from rebuttal; and "
                "engagement by paraphrase — neither naming nor reproducing words — is "
                "missed entirely."
            )
            all_turns = [engine.make_turn(t["speaker"], t["body"], t.get("addressee"))
                         for t in turns]
            tally = engine.engagement_tally(all_turns, agents, humans=humans)

            def _render(section, label):
                if not section:
                    return
                st.markdown(f"**{label}**")
                for speaker, counts in section.items():
                    listed = ", ".join(f"{n} ({c})" for n, c in
                                       sorted(counts.items(), key=lambda kv: -kv[1]))
                    st.markdown(f"- {speaker} → {listed}")

            _render(tally["named"], "By name")
            _render(tally["quoted"], "By quoting their words, without naming them")

            if tally["early"]:
                st.markdown("**Named someone who hadn't spoken yet**")
                st.caption(
                    "Ambiguous by construction: either a forward-looking nomination "
                    "(\"I'd want to push on that with X\") or an attribution to a turn "
                    "that doesn't exist. Worth opening the turn to see which."
                )
                for speaker, target, idx in tally["early"]:
                    st.markdown(f"- turn {idx + 1}: **{speaker}** named **{target}**")

            never = engine.never_engaged(all_turns, agents, humans=humans)
            if never:
                st.markdown("**Neither named nor quoted by anyone:** " + ", ".join(never))

            if not any([tally["named"], tally["quoted"], tally["early"]]):
                st.markdown("*Nobody has addressed anybody by name or quotation yet.*")


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
