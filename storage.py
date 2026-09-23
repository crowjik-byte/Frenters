"""
Storage layer. Two implementations behind one interface:

    JsonStorage      -- local directory, one file per record. For running on
                        your own machine. NOT for Streamlit Community Cloud,
                        which does not guarantee persistence of local files
                        and may delete them at any time.
    PostgresStorage  -- Neon / Supabase / anything speaking Postgres.

Both take and return IDENTICAL dicts. That is the whole discipline of this
file: if the two ever diverge in shape, migrating from JSON to Postgres
becomes a transform instead of a loop. get_storage() picks one from the
environment, so nothing above this layer knows which is running.

Ids are UUIDs from the start, not sequential integers, so they carry over
as primary keys unchanged and a dialogue's share link survives the
migration.

RECORD SHAPES
-------------
participant: id, schema_version, display_name, app_token, created_at
agent:       id, schema_version, name, persona, description, kind,
             visibility, created_by, source_question, created_at
dialogue:    id, schema_version, title, question, agents (embedded list),
             sweep (dict or None), created_by, created_at
turn:        id, dialogue_id, seq, speaker, body, addressee, author_id,
             created_at

A dialogue EMBEDS its roster rather than referencing agent ids. Library
agents get edited; a transcript must stay coherent with the personas that
actually produced it. Same decoupled-snapshot reasoning as the agent
library in the earlier browser tool.
"""

import json
import os
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def new_id():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# INTERFACE
# ---------------------------------------------------------------------------

class Storage:
    """Subclasses must return plain dicts in the shapes documented above."""

    # -- participants --
    def upsert_participant(self, display_name, app_token, participant_id=None): raise NotImplementedError
    def participant_by_token(self, app_token): raise NotImplementedError
    def list_participants(self): raise NotImplementedError

    # -- agents --
    def save_agent(self, agent, created_by, source_question=None,
                   visibility="personal", agent_id=None): raise NotImplementedError
    def get_agent(self, agent_id): raise NotImplementedError
    def list_agents(self, created_by=None, include_public=True): raise NotImplementedError
    def delete_agent(self, agent_id): raise NotImplementedError

    # -- dialogues --
    def create_dialogue(self, title, question, agents, created_by, sweep=None): raise NotImplementedError
    def get_dialogue(self, dialogue_id): raise NotImplementedError
    def list_dialogues(self, created_by=None): raise NotImplementedError
    def update_dialogue(self, dialogue_id, **fields): raise NotImplementedError

    # -- turns (append-only) --
    def append_turn(self, dialogue_id, speaker, body, addressee=None, author_id=None): raise NotImplementedError
    def list_turns(self, dialogue_id): raise NotImplementedError


# ---------------------------------------------------------------------------
# LOCAL JSON
# ---------------------------------------------------------------------------

class JsonStorage(Storage):
    """
    One file per record, never one big blob: concurrent writes don't clobber
    each other, and migrating is a directory walk.
    """

    def __init__(self, root="./data"):
        self.root = root
        for sub in ("participants", "agents", "dialogues", "turns"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)

    # -- helpers --
    def _path(self, kind, rec_id):
        return os.path.join(self.root, kind, f"{rec_id}.json")

    def _write(self, kind, rec):
        tmp = self._path(kind, rec["id"]) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, self._path(kind, rec["id"]))  # atomic
        return rec

    def _read(self, kind, rec_id):
        try:
            with open(self._path(kind, rec_id)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _read_all(self, kind):
        d = os.path.join(self.root, kind)
        out = []
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn)) as f:
                    out.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue  # a half-written or hand-mangled file shouldn't break a listing
        return out

    # -- participants --
    def upsert_participant(self, display_name, app_token, participant_id=None):
        existing = self.participant_by_token(app_token)
        rec = {
            "id": existing["id"] if existing else (participant_id or new_id()),
            "schema_version": SCHEMA_VERSION,
            "display_name": display_name,
            "app_token": app_token,
            "created_at": existing["created_at"] if existing else now(),
        }
        return self._write("participants", rec)

    def participant_by_token(self, app_token):
        for p in self._read_all("participants"):
            if p["app_token"] == app_token:
                return p
        return None

    def list_participants(self):
        return sorted(self._read_all("participants"), key=lambda p: p["created_at"])

    # -- agents --
    def save_agent(self, agent, created_by, source_question=None,
                   visibility="personal", agent_id=None):
        rec = {
            "id": agent_id or new_id(),
            "schema_version": SCHEMA_VERSION,
            "name": agent["name"],
            "persona": agent["persona"],
            "description": agent["description"],
            "kind": agent["kind"],
            "visibility": visibility,
            "created_by": created_by,
            "source_question": source_question,
            "created_at": now(),
        }
        return self._write("agents", rec)

    def get_agent(self, agent_id):
        return self._read("agents", agent_id)

    def list_agents(self, created_by=None, include_public=True):
        out = []
        for a in self._read_all("agents"):
            mine = created_by is not None and a["created_by"] == created_by
            public = include_public and a.get("visibility") == "public"
            if created_by is None or mine or public:
                out.append(a)
        return sorted(out, key=lambda a: a["name"].lower())

    def delete_agent(self, agent_id):
        try:
            os.remove(self._path("agents", agent_id))
            return True
        except FileNotFoundError:
            return False

    # -- dialogues --
    def create_dialogue(self, title, question, agents, created_by, sweep=None):
        rec = {
            "id": new_id(),
            "schema_version": SCHEMA_VERSION,
            "title": title,
            "question": question,
            "agents": agents,
            "sweep": sweep,
            "created_by": created_by,
            "created_at": now(),
        }
        os.makedirs(os.path.join(self.root, "turns", rec["id"]), exist_ok=True)
        return self._write("dialogues", rec)

    def get_dialogue(self, dialogue_id):
        return self._read("dialogues", dialogue_id)

    def list_dialogues(self, created_by=None):
        out = [d for d in self._read_all("dialogues")
               if created_by is None or d["created_by"] == created_by]
        return sorted(out, key=lambda d: d["created_at"], reverse=True)

    def update_dialogue(self, dialogue_id, **fields):
        rec = self.get_dialogue(dialogue_id)
        if rec is None:
            raise KeyError(dialogue_id)
        rec.update(fields)
        return self._write("dialogues", rec)

    # -- turns --
    def _turn_dir(self, dialogue_id):
        d = os.path.join(self.root, "turns", dialogue_id)
        os.makedirs(d, exist_ok=True)
        return d

    def append_turn(self, dialogue_id, speaker, body, addressee=None, author_id=None):
        d = self._turn_dir(dialogue_id)
        seq = len([f for f in os.listdir(d) if f.endswith(".json")])
        rec = {
            "id": new_id(),
            "dialogue_id": dialogue_id,
            "seq": seq,
            "speaker": speaker,
            "body": body,
            "addressee": addressee,
            "author_id": author_id,
            "created_at": now(),
        }
        # seq-prefixed filename so a directory listing sorts correctly
        path = os.path.join(d, f"{seq:05d}-{rec['id']}.json")
        with open(path, "w") as f:
            json.dump(rec, f, indent=2)
        return rec

    def list_turns(self, dialogue_id):
        d = self._turn_dir(dialogue_id)
        out = []
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn)) as f:
                    out.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(out, key=lambda t: t["seq"])


# ---------------------------------------------------------------------------
# POSTGRES
#
# Untested against a live database -- the SQL is straightforward but nothing
# here has been run. Call init_schema() once before first use.
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS participants (
    id              UUID PRIMARY KEY,
    schema_version  INT NOT NULL,
    display_name    TEXT NOT NULL,
    app_token       TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agents (
    id              UUID PRIMARY KEY,
    schema_version  INT NOT NULL,
    name            TEXT NOT NULL,
    persona         TEXT NOT NULL,
    description     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    visibility      TEXT NOT NULL DEFAULT 'personal',
    created_by      UUID,
    source_question TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agents_created_by_idx ON agents (created_by);
CREATE INDEX IF NOT EXISTS agents_visibility_idx ON agents (visibility);

CREATE TABLE IF NOT EXISTS dialogues (
    id              UUID PRIMARY KEY,
    schema_version  INT NOT NULL,
    title           TEXT NOT NULL,
    question        TEXT NOT NULL,
    agents          JSONB NOT NULL,
    sweep           JSONB,
    created_by      UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dialogues_created_by_idx ON dialogues (created_by);

CREATE TABLE IF NOT EXISTS turns (
    id              UUID PRIMARY KEY,
    dialogue_id     UUID NOT NULL REFERENCES dialogues(id) ON DELETE CASCADE,
    seq             INT NOT NULL,
    speaker         TEXT NOT NULL,
    body            TEXT NOT NULL,
    addressee       TEXT,
    author_id       UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dialogue_id, seq)
);
CREATE INDEX IF NOT EXISTS turns_dialogue_idx ON turns (dialogue_id, seq);
"""


class PostgresStorage(Storage):

    def __init__(self, dsn):
        import psycopg
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn

    def _conn(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def init_schema(self):
        with self._conn() as c, c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            c.commit()

    @staticmethod
    def _clean(row):
        """Normalize to the same shape JsonStorage returns."""
        if row is None:
            return None
        row = dict(row)
        for k, v in list(row.items()):
            if isinstance(v, uuid.UUID):
                row[k] = str(v)
            elif isinstance(v, datetime):
                row[k] = v.isoformat()
        return row

    # -- participants --
    def upsert_participant(self, display_name, app_token, participant_id=None):
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO participants (id, schema_version, display_name, app_token)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (app_token) DO UPDATE SET display_name = EXCLUDED.display_name
                   RETURNING *""",
                (participant_id or new_id(), SCHEMA_VERSION, display_name, app_token),
            )
            row = cur.fetchone()
            c.commit()
        return self._clean(row)

    def participant_by_token(self, app_token):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM participants WHERE app_token = %s", (app_token,))
            return self._clean(cur.fetchone())

    def list_participants(self):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM participants ORDER BY created_at")
            return [self._clean(r) for r in cur.fetchall()]

    # -- agents --
    def save_agent(self, agent, created_by, source_question=None,
                   visibility="personal", agent_id=None):
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO agents (id, schema_version, name, persona, description,
                                       kind, visibility, created_by, source_question)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (agent_id or new_id(), SCHEMA_VERSION, agent["name"], agent["persona"],
                 agent["description"], agent["kind"], visibility, created_by, source_question),
            )
            row = cur.fetchone()
            c.commit()
        return self._clean(row)

    def get_agent(self, agent_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM agents WHERE id = %s", (agent_id,))
            return self._clean(cur.fetchone())

    def list_agents(self, created_by=None, include_public=True):
        with self._conn() as c, c.cursor() as cur:
            if created_by is None:
                cur.execute("SELECT * FROM agents ORDER BY lower(name)")
            elif include_public:
                cur.execute(
                    "SELECT * FROM agents WHERE created_by = %s OR visibility = 'public' "
                    "ORDER BY lower(name)", (created_by,))
            else:
                cur.execute("SELECT * FROM agents WHERE created_by = %s ORDER BY lower(name)",
                            (created_by,))
            return [self._clean(r) for r in cur.fetchall()]

    def delete_agent(self, agent_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
            deleted = cur.rowcount > 0
            c.commit()
        return deleted

    # -- dialogues --
    def create_dialogue(self, title, question, agents, created_by, sweep=None):
        from psycopg.types.json import Jsonb
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO dialogues (id, schema_version, title, question,
                                          agents, sweep, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (new_id(), SCHEMA_VERSION, title, question,
                 Jsonb(agents), Jsonb(sweep) if sweep else None, created_by),
            )
            row = cur.fetchone()
            c.commit()
        return self._clean(row)

    def get_dialogue(self, dialogue_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM dialogues WHERE id = %s", (dialogue_id,))
            return self._clean(cur.fetchone())

    def list_dialogues(self, created_by=None):
        with self._conn() as c, c.cursor() as cur:
            if created_by is None:
                cur.execute("SELECT * FROM dialogues ORDER BY created_at DESC")
            else:
                cur.execute("SELECT * FROM dialogues WHERE created_by = %s "
                            "ORDER BY created_at DESC", (created_by,))
            return [self._clean(r) for r in cur.fetchall()]

    def update_dialogue(self, dialogue_id, **fields):
        from psycopg.types.json import Jsonb
        allowed = {"title", "question", "agents", "sweep"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Cannot update field {k!r}")
            sets.append(f"{k} = %s")
            vals.append(Jsonb(v) if k in ("agents", "sweep") else v)
        vals.append(dialogue_id)
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"UPDATE dialogues SET {', '.join(sets)} WHERE id = %s RETURNING *", vals)
            row = cur.fetchone()
            c.commit()
        return self._clean(row)

    # -- turns --
    def append_turn(self, dialogue_id, speaker, body, addressee=None, author_id=None):
        """
        seq is assigned inside the transaction from the current max, and
        UNIQUE (dialogue_id, seq) means a concurrent append fails loudly
        rather than silently overwriting. Retry once on collision.
        """
        for _ in range(3):
            try:
                with self._conn() as c, c.cursor() as cur:
                    cur.execute("SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM turns "
                                "WHERE dialogue_id = %s", (dialogue_id,))
                    seq = cur.fetchone()["next"]
                    cur.execute(
                        """INSERT INTO turns (id, dialogue_id, seq, speaker, body,
                                              addressee, author_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (new_id(), dialogue_id, seq, speaker, body, addressee, author_id),
                    )
                    row = cur.fetchone()
                    c.commit()
                return self._clean(row)
            except self._psycopg.errors.UniqueViolation:
                continue
        raise RuntimeError("Could not append turn after 3 attempts (seq collision).")

    def list_turns(self, dialogue_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM turns WHERE dialogue_id = %s ORDER BY seq",
                        (dialogue_id,))
            return [self._clean(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# SELECTION + MIGRATION
# ---------------------------------------------------------------------------

def get_storage(dsn=None, json_root=None):
    """
    Postgres if a DSN is given or DATABASE_URL is set; local JSON otherwise.
    Nothing above this layer needs to know which.
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if dsn:
        return PostgresStorage(dsn)
    return JsonStorage(json_root or os.environ.get("DIALOGUE_DATA_DIR", "./data"))


def migrate_json_to_postgres(json_root, dsn, verbose=True):
    """
    One-shot. Ids are preserved, so share links stay valid across the move.
    Safe to run against a fresh database; not idempotent -- running it twice
    will raise on duplicate primary keys rather than silently double-write.
    """
    src = JsonStorage(json_root)
    dst = PostgresStorage(dsn)
    dst.init_schema()

    counts = {"participants": 0, "agents": 0, "dialogues": 0, "turns": 0}

    for p in src.list_participants():
        dst.upsert_participant(p["display_name"], p["app_token"], participant_id=p["id"])
        counts["participants"] += 1

    for a in src.list_agents():
        dst.save_agent(a, a.get("created_by"), source_question=a.get("source_question"),
                       visibility=a.get("visibility", "personal"), agent_id=a["id"])
        counts["agents"] += 1

    from psycopg.types.json import Jsonb
    for d in src.list_dialogues():
        with dst._conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO dialogues (id, schema_version, title, question,
                                          agents, sweep, created_by, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (d["id"], d.get("schema_version", SCHEMA_VERSION), d["title"], d["question"],
                 Jsonb(d["agents"]), Jsonb(d["sweep"]) if d.get("sweep") else None,
                 d.get("created_by"), d["created_at"]),
            )
            c.commit()
        counts["dialogues"] += 1

        for t in src.list_turns(d["id"]):
            with dst._conn() as c, c.cursor() as cur:
                cur.execute(
                    """INSERT INTO turns (id, dialogue_id, seq, speaker, body,
                                          addressee, author_id, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (t["id"], t["dialogue_id"], t["seq"], t["speaker"], t["body"],
                     t.get("addressee"), t.get("author_id"), t["created_at"]),
                )
                c.commit()
            counts["turns"] += 1

    if verbose:
        print("Migrated:", ", ".join(f"{v} {k}" for k, v in counts.items()))
    return counts
