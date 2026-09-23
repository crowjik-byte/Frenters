# Human-Directed Dialogue

A dialogue tool where you drive every turn. Several named perspectives, one
question, and you decide who speaks next and what about. See `DESIGN.md` for
the decisions behind it and what's still open.

## Files

| | |
|---|---|
| `engine.py` | Pure Python. Agents, transcript, perspective sweep/selection, agent & moderator & summarizer calls. No Streamlit, no storage. |
| `storage.py` | `JsonStorage` (local dir) and `PostgresStorage` behind one interface, plus `migrate_json_to_postgres`. |
| `app.py` | Streamlit UI. |
| `DESIGN.md` | Why it's built this way. |

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
export OWNER_TOKEN=pick-something-unguessable
export OWNER_NAME=YourName
streamlit run app.py
```

Then open `http://localhost:8501/?k=<OWNER_TOKEN>`.

With no `DATABASE_URL` set, it writes to `./data` as one JSON file per
record. Fine locally. **Not** fine on Streamlit Community Cloud, which does
not guarantee persistence of local files and may delete them at any time.

## Moving to Postgres

```bash
export DATABASE_URL='postgresql://...'   # Neon, Supabase, anything
python -c "import storage; storage.PostgresStorage('$DATABASE_URL').init_schema()"
```

`get_storage()` switches automatically once `DATABASE_URL` is set. To carry
local dialogues over:

```bash
python -c "import storage; storage.migrate_json_to_postgres('./data', '$DATABASE_URL')"
```

Ids are preserved, so share links stay valid. Not idempotent — run it once
against a fresh database.

## Deploying

Public app, access scoped by link. Put `ANTHROPIC_API_KEY`, `DATABASE_URL`,
`OWNER_TOKEN` and `OWNER_NAME` in Streamlit secrets.

- `?k=<app-token>` — full app: create dialogues, sweep, manage agents.
- `?d=<dialogue-id>` — one dialogue: read and add turns, nothing else.
- Neither — landing page.

Issue a different app token per person (add rows to `participants`) so the
app knows who's who without anyone logging in. This filters drive-bys; it
is not real security. Set a spend limit in the Anthropic console.
