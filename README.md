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

Get the connection string from your provider's dashboard (on Neon: **Connect**
on the Project Dashboard). You want **two** versions of it — they differ only
in the hostname:

- **Pooled** (`-pooler` in the host) → `DATABASE_URL` for the running app.
  `PostgresStorage` opens a connection per operation, which is what pooling
  is for.
- **Direct** (no `-pooler`) → for schema creation and migration only.
  PgBouncer transaction mode breaks session-level features.

Keep `sslmode=require` in the string. Never commit it — `.env` locally,
Streamlit secrets when deployed.

```bash
# schema creation: use the DIRECT string
python -c "import storage; storage.PostgresStorage('<DIRECT_URL>').init_schema()"

# then point the app at the POOLED string
export DATABASE_URL='<POOLED_URL>'
```

`get_storage()` switches automatically once `DATABASE_URL` is set. To carry
local dialogues over:

```bash
python -c "import storage; storage.migrate_json_to_postgres('./data', '<DIRECT_URL>')"
```

Ids are preserved, so share links stay valid. Not idempotent — run it once
against a fresh database.

## Upgrading an existing database

`init_schema()` is idempotent and includes the `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS` statements for columns added after the first release
(`selection`, `reference`). Re-running it against a live database is safe:

```bash
python -c "import storage; storage.PostgresStorage('<DIRECT_URL>').init_schema()"
```

Dialogues created before a column existed simply have it null — an older
dialogue shows no expected-tensions panel and no reference material, which
is correct rather than broken.

## Deploying

Public app, access scoped by link. Put `ANTHROPIC_API_KEY`, `DATABASE_URL`,
`OWNER_TOKEN` and `OWNER_NAME` in Streamlit secrets.

- `?k=<app-token>` — full app: create dialogues, sweep, manage agents.
- `?d=<dialogue-id>` — one dialogue: read and add turns, nothing else.
- Neither — landing page.

Issue a different app token per person (add rows to `participants`) so the
app knows who's who without anyone logging in. This filters drive-bys; it
is not real security. Set a spend limit in the Anthropic console.
