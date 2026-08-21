import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
from werkzeug.security import check_password_hash, generate_password_hash

psycopg2.extras.register_uuid()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

DEFAULT_STEPS = [
    {"name": "Intro Meeting", "description": "Initial consultation to discuss goals and gather information.", "sort_order": 0},
    {"name": "Preliminary Review", "description": "Review of existing documents and financial overview.", "sort_order": 1},
    {"name": "Design Meeting", "description": "Collaborative session to outline the estate plan structure.", "sort_order": 2},
    {"name": "Structural Planning", "description": "Detailed planning of trusts, entities, and asset protection.", "sort_order": 3},
    {"name": "Drafting", "description": "Preparation of all legal documents.", "sort_order": 4},
    {"name": "Final Review", "description": "Review of drafted documents with the client.", "sort_order": 5},
    {"name": "Execution", "description": "Formal signing and notarization of all documents.", "sort_order": 6},
    {"name": "Ongoing Service", "description": "Continued support, updates, and maintenance of the estate plan.", "sort_order": 7},
]


@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS = [
    # v0: firms table + initial schema (multi-tenant from day one)
    [
        """CREATE TABLE IF NOT EXISTS firms (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            access_code_hash TEXT NOT NULL,
            access_code_plain TEXT NOT NULL DEFAULT '',
            tracker_access_code_hash TEXT NOT NULL DEFAULT '',
            tracker_access_code_plain TEXT NOT NULL DEFAULT '',
            config JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS clients (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id),
            client_name TEXT NOT NULL,
            client_id_code TEXT NOT NULL,
            access_code_hash TEXT NOT NULL,
            access_code_plain TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (firm_id, client_id_code)
        )""",
        """CREATE TABLE IF NOT EXISTS client_steps (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            sort_order INT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'complete')),
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_client_steps_client ON client_steps(client_id)",
        """CREATE TABLE IF NOT EXISTS ai_usage_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            provider TEXT NOT NULL,
            model TEXT,
            project TEXT NOT NULL,
            tool TEXT NOT NULL,
            status TEXT NOT NULL,
            input_tokens INT,
            output_tokens INT,
            reasoning_tokens INT,
            pages_processed INT,
            cost_usd NUMERIC(10,6),
            execution_ms INT,
            notes TEXT,
            firm_id UUID REFERENCES firms(id)
        )""",
    ],
    [
        """ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id)""",
    ],
    [
        """ALTER TABLE clients ADD COLUMN IF NOT EXISTS firm_id UUID REFERENCES firms(id)""",
        """UPDATE clients SET firm_id = (SELECT id FROM firms ORDER BY created_at LIMIT 1) WHERE firm_id IS NULL""",
        """DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'clients_firm_id_client_id_code_key'
            ) THEN
                BEGIN
                    ALTER TABLE clients ADD CONSTRAINT clients_firm_id_client_id_code_key UNIQUE (firm_id, client_id_code);
                EXCEPTION WHEN others THEN NULL;
                END;
            END IF;
        END $$""",
    ],
    # v3: allow firm deletion without losing ai_usage_log rows
    [
        """ALTER TABLE ai_usage_log DROP CONSTRAINT IF EXISTS ai_usage_log_firm_id_fkey""",
        """ALTER TABLE ai_usage_log ADD CONSTRAINT ai_usage_log_firm_id_fkey
           FOREIGN KEY (firm_id) REFERENCES firms(id) ON DELETE SET NULL""",
    ],
    # v4: login attempts log
    [
        """CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ip_address TEXT NOT NULL,
            access_code_used TEXT NOT NULL DEFAULT '',
            firm_name TEXT,
            firm_slug TEXT,
            success BOOLEAN NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(attempted_at DESC)",
    ],
    # v5: specimen documents for doc differences tool
    [
        """CREATE TABLE IF NOT EXISTS specimen_documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            docx_data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_specimen_documents_firm ON specimen_documents(firm_id)",
    ],
    # v6: optional description on specimen documents
    [
        "ALTER TABLE specimen_documents ADD COLUMN IF NOT EXISTS description TEXT",
    ],
    # v7: optional employee logins
    [
        """ALTER TABLE firms ADD COLUMN IF NOT EXISTS require_employee_login
           BOOLEAN NOT NULL DEFAULT false""",
        """CREATE TABLE IF NOT EXISTS employees (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            employee_id_code TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (firm_id, employee_id_code)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_employees_firm ON employees(firm_id)",
        "ALTER TABLE login_attempts ADD COLUMN IF NOT EXISTS employee_id_code TEXT",
        "ALTER TABLE login_attempts ADD COLUMN IF NOT EXISTS employee_name TEXT",
    ],
    # v8: employee attribution on AI usage log
    [
        """ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS employee_id UUID
           REFERENCES employees(id) ON DELETE SET NULL""",
        "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS employee_id_code TEXT",
        "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS employee_name TEXT",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_log_ts ON ai_usage_log (timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_log_firm_ts ON ai_usage_log (firm_id, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_log_employee_ts ON ai_usage_log (employee_id, timestamp DESC)",
    ],
]


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS _schema_version (
                    version INT NOT NULL DEFAULT -1
                )
            """)
            cur.execute("SELECT version FROM _schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO _schema_version (version) VALUES (-1)")
                current = -1
            else:
                current = row[0]
                if current >= 0:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'firms'
                        )
                    """)
                    if not cur.fetchone()[0]:
                        cur.execute("UPDATE _schema_version SET version = -1")
                        current = -1

            for i, statements in enumerate(MIGRATIONS):
                if i > current:
                    for sql in statements:
                        cur.execute(sql)
                    cur.execute("UPDATE _schema_version SET version = %s", (i,))


def seed_firm_if_empty():
    """Seed GW Law as the first firm if no firms exist yet."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM firms")
            if cur.fetchone()[0] > 0:
                return

    ep_schema_path = Path(__file__).parent / "estate_design_meeting_schema.json"
    prospect_schema_path = Path(__file__).parent / "prospect_schema.json"

    ep_schema = {}
    prospect_schema = {}
    if ep_schema_path.exists():
        with open(ep_schema_path) as f:
            ep_schema = json.load(f)
    if prospect_schema_path.exists():
        with open(prospect_schema_path) as f:
            prospect_schema = json.load(f)

    config = {
        "firm_context": (
            "This tool is used by Galbraith Weatherbie Law, an estate planning "
            "firm operating in Florida and Indiana. The managing partner is Brad "
            "Galbraith. Use correct spellings of the firm name and personnel in "
            "any output."
        ),
        "ep_schema": ep_schema,
        "prospect_schema": prospect_schema,
        "doc_separator_rules": "",
        "doc_filename_format": "{last}, {first} - {type} {date}",
        "tracker_default_steps": DEFAULT_STEPS,
        "client_site_url": "https://gwclient.com",
        "tools_enabled": {
            "drafting_notes": True,
            "doc_separator": True,
            "prospect_summarizer": True,
            "tracker": True,
        },
    }

    access_code = os.environ.get("SEED_ACCESS_CODE", "changeme")
    tracker_code = os.environ.get("SEED_TRACKER_CODE", "changeme")

    create_firm(
        name="Galbraith Weatherbie Law",
        slug="gw-law",
        access_code=access_code,
        tracker_access_code=tracker_code,
        config=config,
    )


# ---------------------------------------------------------------------------
# Firms

# ---------------------------------------------------------------------------

def create_firm(name, slug, access_code, tracker_access_code, config=None,
                require_employee_login=False):
    firm_id = uuid.uuid4()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO firms (id, name, slug, access_code_hash, access_code_plain,
                   tracker_access_code_hash, tracker_access_code_plain, config,
                   require_employee_login)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    firm_id, name, slug,
                    generate_password_hash(access_code), access_code,
                    generate_password_hash(tracker_access_code), tracker_access_code,
                    json.dumps(config or {}),
                    bool(require_employee_login),
                ),
            )
    return str(firm_id)


def get_firm(firm_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, name, slug, config, access_code_plain, tracker_access_code_plain,
                          require_employee_login FROM firms WHERE id = %s""",
                (firm_id,),
            )
            row = cur.fetchone()
            if row and isinstance(row["config"], str):
                row["config"] = json.loads(row["config"])
            return row


def get_firm_config(firm_id):
    firm = get_firm(firm_id)
    if not firm:
        raise ValueError(f"Firm {firm_id} not found")
    config = firm.get("config")
    if not config:
        raise ValueError(f"Firm {firm_id} has no configuration")
    return config


def lookup_firm_by_access_code(access_code):
    """Find a firm by checking the access code against all firms. Returns firm dict or None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, name, slug, access_code_hash, config, require_employee_login
                   FROM firms ORDER BY created_at"""
            )
            for row in cur.fetchall():
                if check_password_hash(row["access_code_hash"], access_code):
                    if isinstance(row["config"], str):
                        row["config"] = json.loads(row["config"])
                    return row
    return None


def list_firms():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, slug, created_at FROM firms ORDER BY name")
            return cur.fetchall()


def delete_firm(firm_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM employees WHERE firm_id = %s", (firm_id,))
            cur.execute("DELETE FROM client_steps WHERE client_id IN (SELECT id FROM clients WHERE firm_id = %s)", (firm_id,))
            cur.execute("DELETE FROM clients WHERE firm_id = %s", (firm_id,))
            cur.execute("UPDATE ai_usage_log SET firm_id = NULL WHERE firm_id = %s", (firm_id,))
            cur.execute("DELETE FROM firms WHERE id = %s", (firm_id,))


def update_firm(firm_id, *, name=None, slug=None, access_code=None, tracker_access_code=None,
                config=None, require_employee_login=None):
    sets, params = [], []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if slug is not None:
        sets.append("slug = %s")
        params.append(slug)
    if access_code is not None:
        sets.append("access_code_hash = %s")
        params.append(generate_password_hash(access_code))
        sets.append("access_code_plain = %s")
        params.append(access_code)
    if tracker_access_code is not None:
        sets.append("tracker_access_code_hash = %s")
        params.append(generate_password_hash(tracker_access_code))
        sets.append("tracker_access_code_plain = %s")
        params.append(tracker_access_code)
    if config is not None:
        sets.append("config = %s")
        params.append(json.dumps(config))
    if require_employee_login is not None:
        sets.append("require_employee_login = %s")
        params.append(bool(require_employee_login))
    if not sets:
        return
    sets.append("updated_at = now()")
    params.append(firm_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE firms SET {', '.join(sets)} WHERE id = %s", params)


# ---------------------------------------------------------------------------
# Employees (scoped to firm)
# ---------------------------------------------------------------------------

def firm_requires_employee_login(firm_id):
    """True/False for the flag, or None if the firm does not exist."""
    try:
        firm_uuid = uuid.UUID(str(firm_id))
    except (ValueError, TypeError, AttributeError):
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT require_employee_login FROM firms WHERE id = %s",
                (firm_uuid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return bool(row[0])


def list_employees(firm_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, firm_id, employee_id_code, name, created_at, updated_at
                   FROM employees WHERE firm_id = %s
                   ORDER BY name, employee_id_code""",
                (firm_id,),
            )
            return cur.fetchall()


def get_employee_by_code(firm_id, employee_id_code):
    """Case-sensitive exact match on employee_id_code."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, firm_id, employee_id_code, name
                   FROM employees WHERE firm_id = %s AND employee_id_code = %s""",
                (firm_id, employee_id_code),
            )
            return cur.fetchone()


def create_employee(firm_id, employee_id_code, name):
    emp_id = uuid.uuid4()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO employees (id, firm_id, employee_id_code, name)
                   VALUES (%s, %s, %s, %s)""",
                (emp_id, firm_id, employee_id_code, name),
            )
    return str(emp_id)


def upsert_employees(firm_id, rows):
    """rows: list of (employee_id_code, name). Upsert by (firm_id, employee_id_code)."""
    if not rows:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO employees (firm_id, employee_id_code, name)
                   VALUES %s
                   ON CONFLICT (firm_id, employee_id_code) DO UPDATE
                   SET name = EXCLUDED.name, updated_at = now()""",
                [(firm_id, code, name) for code, name in rows],
            )
            return len(rows)


def delete_employee(employee_id, firm_id=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            sql = "DELETE FROM employees WHERE id = %s"
            params = [employee_id]
            if firm_id:
                sql += " AND firm_id = %s"
                params.append(firm_id)
            cur.execute(sql, params)
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Clients (scoped to firm)
# ---------------------------------------------------------------------------

def list_clients(firm_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id, c.client_name, c.client_id_code, c.created_at, c.updated_at,
                    (SELECT cs.name FROM client_steps cs
                     WHERE cs.client_id = c.id AND cs.status = 'active'
                     ORDER BY cs.sort_order LIMIT 1) AS current_step,
                    (SELECT count(*) FROM client_steps cs
                     WHERE cs.client_id = c.id AND cs.status = 'complete') AS completed_count,
                    (SELECT count(*) FROM client_steps cs
                     WHERE cs.client_id = c.id) AS total_steps
                FROM clients c
                WHERE c.firm_id = %s
                ORDER BY c.updated_at DESC
            """, (firm_id,))
            return cur.fetchall()


def get_client(client_id, firm_id=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = "SELECT id, firm_id, client_name, client_id_code, access_code_plain, created_at, updated_at FROM clients WHERE id = %s"
            params = [client_id]
            if firm_id:
                sql += " AND firm_id = %s"
                params.append(firm_id)
            cur.execute(sql, params)
            return cur.fetchone()


def create_client(firm_id, client_name, client_id_code, access_code):
    client_id = uuid.uuid4()
    pw_hash = generate_password_hash(access_code)

    firm_config = get_firm_config(firm_id)
    steps = firm_config.get("tracker_default_steps") or DEFAULT_STEPS

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clients (id, firm_id, client_name, client_id_code, access_code_hash, access_code_plain) VALUES (%s, %s, %s, %s, %s, %s)",
                (client_id, firm_id, client_name, client_id_code, pw_hash, access_code),
            )
            for s in steps:
                cur.execute(
                    "INSERT INTO client_steps (client_id, name, description, sort_order) VALUES (%s, %s, %s, %s)",
                    (client_id, s["name"], s["description"], s["sort_order"]),
                )
    return str(client_id)


def update_client(client_id, *, client_name=None, client_id_code=None, access_code=None):
    sets, params = [], []
    if client_name is not None:
        sets.append("client_name = %s")
        params.append(client_name)
    if client_id_code is not None:
        sets.append("client_id_code = %s")
        params.append(client_id_code)
    if access_code is not None:
        sets.append("access_code_hash = %s")
        params.append(generate_password_hash(access_code))
        sets.append("access_code_plain = %s")
        params.append(access_code)
    if not sets:
        return
    sets.append("updated_at = now()")
    params.append(client_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = %s", params)


def delete_client(client_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clients WHERE id = %s", (client_id,))


# ---------------------------------------------------------------------------
# Client steps
# ---------------------------------------------------------------------------

def get_client_steps(client_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, name, description, notes, sort_order, status, completed_at
                   FROM client_steps WHERE client_id = %s ORDER BY sort_order""",
                (client_id,),
            )
            return cur.fetchall()


def add_client_step(client_id, name, description="", sort_order=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if sort_order is None:
                cur.execute("SELECT coalesce(max(sort_order), -1) + 1 FROM client_steps WHERE client_id = %s", (client_id,))
                sort_order = cur.fetchone()[0]
            step_id = uuid.uuid4()
            cur.execute(
                "INSERT INTO client_steps (id, client_id, name, description, sort_order) VALUES (%s, %s, %s, %s, %s)",
                (step_id, client_id, name, description, sort_order),
            )
            return str(step_id)


def update_step(step_id, **kwargs):
    allowed = {"name", "description", "notes", "sort_order", "status"}
    sets, params = [], []
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = %s")
        params.append(v)
    if "status" in kwargs and kwargs["status"] == "complete":
        sets.append("completed_at = now()")
    elif "status" in kwargs and kwargs["status"] != "complete":
        sets.append("completed_at = NULL")
    if not sets:
        return
    params.append(step_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE client_steps SET {', '.join(sets)} WHERE id = %s", params)
            cur.execute(
                "UPDATE clients SET updated_at = now() WHERE id = (SELECT client_id FROM client_steps WHERE id = %s)",
                (step_id,),
            )


def delete_step(step_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clients SET updated_at = now() WHERE id = (SELECT client_id FROM client_steps WHERE id = %s)",
                (step_id,),
            )
            cur.execute("DELETE FROM client_steps WHERE id = %s", (step_id,))


def reorder_steps(client_id, step_ids):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i, sid in enumerate(step_ids):
                cur.execute(
                    "UPDATE client_steps SET sort_order = %s WHERE id = %s AND client_id = %s",
                    (i, sid, client_id),
                )
            cur.execute("UPDATE clients SET updated_at = now() WHERE id = %s", (client_id,))


# ---------------------------------------------------------------------------
# Login attempts log
# ---------------------------------------------------------------------------

def log_login_attempt(ip_address, access_code_used, firm_name, firm_slug, success,
                      employee_id_code=None, employee_name=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO login_attempts (ip_address, access_code_used, firm_name, firm_slug,
                   success, employee_id_code, employee_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (ip_address, access_code_used, firm_name, firm_slug, success,
                 employee_id_code, employee_name),
            )


def get_login_attempts(limit=200, offset=0, firm_slug=None, success=None):
    clauses, params = [], []
    if firm_slug:
        clauses.append("firm_slug = %s")
        params.append(firm_slug)
    if success is not None:
        clauses.append("success = %s")
        params.append(success)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM login_attempts {where} ORDER BY attempted_at DESC LIMIT %s OFFSET %s", params)
            return cur.fetchall()


# ---------------------------------------------------------------------------
# AI usage (admin)
# ---------------------------------------------------------------------------

def _usage_filters(days=None, firm_id=None, employee_code=None):
    clauses, params = [], []
    if days:
        clauses.append("l.timestamp >= NOW() - (%s * INTERVAL '1 day')")
        params.append(int(days))
    if firm_id:
        clauses.append("l.firm_id = %s")
        params.append(firm_id)
    if employee_code:
        clauses.append("l.employee_id_code = %s")
        params.append(employee_code)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def usage_overview(days=None, firm_id=None, employee_code=None):
    extra, extra_params = [], []
    if firm_id:
        extra.append("l.firm_id = %s")
        extra_params.append(firm_id)
    if employee_code:
        extra.append("l.employee_id_code = %s")
        extra_params.append(employee_code)
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if days:
                days = int(days)
                cur.execute(
                    f"""SELECT
                            COUNT(*) FILTER (WHERE l.timestamp >= b.cutoff) AS calls,
                            COUNT(*) FILTER (WHERE l.timestamp >= b.cutoff
                                             AND l.status = 'error') AS errors,
                            COALESCE(SUM(l.cost_usd) FILTER (
                                WHERE l.timestamp >= b.cutoff), 0) AS cost,
                            COALESCE(SUM(l.pages_processed) FILTER (
                                WHERE l.timestamp >= b.cutoff), 0) AS pages,
                            COALESCE(SUM(l.cost_usd) FILTER (
                                WHERE l.timestamp >= b.cutoff
                                  AND l.provider = 'openai'), 0) AS cost_openai,
                            COALESCE(SUM(l.cost_usd) FILTER (
                                WHERE l.timestamp >= b.cutoff
                                  AND l.provider = 'google_documentai'), 0) AS cost_ocr,
                            COALESCE(SUM(l.cost_usd) FILTER (
                                WHERE l.timestamp < b.cutoff), 0) AS cost_prior
                        FROM ai_usage_log l
                        CROSS JOIN (
                            SELECT NOW() - (%s * INTERVAL '1 day') AS cutoff,
                                   NOW() - (%s * INTERVAL '1 day') AS prior_start
                        ) b
                        WHERE l.timestamp >= b.prior_start
                        {extra_sql}""",
                    [days, days * 2, *extra_params],
                )
            else:
                where, params = _usage_filters(None, firm_id, employee_code)
                cur.execute(
                    f"""SELECT
                            COUNT(*) AS calls,
                            COUNT(*) FILTER (WHERE l.status = 'error') AS errors,
                            COALESCE(SUM(l.cost_usd), 0) AS cost,
                            COALESCE(SUM(l.pages_processed), 0) AS pages,
                            COALESCE(SUM(l.cost_usd) FILTER (WHERE l.provider = 'openai'), 0) AS cost_openai,
                            COALESCE(SUM(l.cost_usd) FILTER (WHERE l.provider = 'google_documentai'), 0) AS cost_ocr,
                            NULL AS cost_prior
                        FROM ai_usage_log l
                        {where}""",
                    params,
                )
            return cur.fetchone()


def usage_cost_by_day(days=None, firm_id=None, employee_code=None):
    where, params = _usage_filters(days, firm_id, employee_code)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT (l.timestamp AT TIME ZONE 'America/New_York')::date AS day,
                           COALESCE(SUM(l.cost_usd), 0) AS cost
                    FROM ai_usage_log l
                    {where}
                    GROUP BY day
                    ORDER BY day""",
                params,
            )
            return cur.fetchall()


def usage_by_tool(days=None, firm_id=None, employee_code=None):
    where, params = _usage_filters(days, firm_id, employee_code)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT l.tool, l.provider,
                           COUNT(*) AS calls,
                           COUNT(*) FILTER (WHERE l.status = 'error') AS errors,
                           COALESCE(SUM(l.cost_usd), 0) AS cost,
                           COALESCE(SUM(l.pages_processed), 0) AS pages
                    FROM ai_usage_log l
                    {where}
                    GROUP BY l.tool, l.provider
                    ORDER BY cost DESC, calls DESC""",
                params,
            )
            return cur.fetchall()


def usage_by_firm(days=None, firm_id=None, employee_code=None):
    where, params = _usage_filters(days, firm_id, employee_code)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT l.firm_id, f.name AS firm_name, f.slug AS firm_slug,
                           COUNT(*) AS calls,
                           COUNT(*) FILTER (WHERE l.status = 'error') AS errors,
                           COALESCE(SUM(l.cost_usd), 0) AS cost,
                           MAX(l.timestamp) AS last_seen
                    FROM ai_usage_log l
                    LEFT JOIN firms f ON f.id = l.firm_id
                    {where}
                    GROUP BY l.firm_id, f.name, f.slug
                    ORDER BY cost DESC, calls DESC""",
                params,
            )
            return cur.fetchall()


def usage_by_employee(days=None, firm_id=None, employee_code=None):
    where, params = _usage_filters(days, firm_id, employee_code)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT l.employee_id, l.employee_id_code, l.employee_name,
                           l.firm_id, f.name AS firm_name,
                           COUNT(*) AS calls,
                           COUNT(*) FILTER (WHERE l.status = 'error') AS errors,
                           COALESCE(SUM(l.cost_usd), 0) AS cost
                    FROM ai_usage_log l
                    LEFT JOIN firms f ON f.id = l.firm_id
                    {where}
                    GROUP BY l.employee_id, l.employee_id_code, l.employee_name,
                             l.firm_id, f.name
                    ORDER BY cost DESC, calls DESC""",
                params,
            )
            return cur.fetchall()


def usage_recent_errors(days=None, firm_id=None, employee_code=None, limit=50):
    where, params = _usage_filters(days, firm_id, employee_code)
    extra = "l.status = 'error'"
    where = f"{where} AND {extra}" if where else f"WHERE {extra}"
    params = list(params) + [int(limit)]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT l.timestamp, l.tool, l.provider, l.notes, l.execution_ms,
                           l.firm_id, f.name AS firm_name,
                           l.employee_id_code, l.employee_name
                    FROM ai_usage_log l
                    LEFT JOIN firms f ON f.id = l.firm_id
                    {where}
                    ORDER BY l.timestamp DESC
                    LIMIT %s""",
                params,
            )
            return cur.fetchall()


def usage_log_rows(days=None, firm_id=None, employee_code=None, limit=10000):
    where, params = _usage_filters(days, firm_id, employee_code)
    params = list(params) + [int(limit)]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT l.timestamp, f.name AS firm_name,
                           l.employee_name, l.employee_id_code,
                           l.tool, l.provider, l.model, l.status,
                           l.cost_usd, l.input_tokens, l.output_tokens,
                           l.reasoning_tokens, l.pages_processed, l.execution_ms
                    FROM ai_usage_log l
                    LEFT JOIN firms f ON f.id = l.firm_id
                    {where}
                    ORDER BY l.timestamp DESC
                    LIMIT %s""",
                params,
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Specimen documents
# ---------------------------------------------------------------------------

def list_specimen_documents(firm_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, firm_id, name, description, created_at FROM specimen_documents WHERE firm_id = %s ORDER BY name",
                (firm_id,),
            )
            return cur.fetchall()


def get_specimen_document(doc_id, firm_id=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = "SELECT id, firm_id, name, description, docx_data, created_at FROM specimen_documents WHERE id = %s"
            params = [doc_id]
            if firm_id:
                sql += " AND firm_id = %s"
                params.append(firm_id)
            cur.execute(sql, params)
            row = cur.fetchone()
            if row and row.get("docx_data"):
                row["docx_data"] = bytes(row["docx_data"])
            return row


def create_specimen_document(firm_id, name, docx_data, description=None):
    doc_id = uuid.uuid4()
    description = (description or "").strip() or None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO specimen_documents (id, firm_id, name, docx_data, description) VALUES (%s, %s, %s, %s, %s)",
                (doc_id, firm_id, name, psycopg2.Binary(docx_data), description),
            )
    return str(doc_id)


def update_specimen_document(doc_id, name, description=None, firm_id=None):
    description = (description or "").strip() or None
    with get_conn() as conn:
        with conn.cursor() as cur:
            sql = "UPDATE specimen_documents SET name = %s, description = %s WHERE id = %s"
            params = [name, description, doc_id]
            if firm_id:
                sql += " AND firm_id = %s"
                params.append(firm_id)
            cur.execute(sql, params)
            return cur.rowcount > 0


def delete_specimen_document(doc_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM specimen_documents WHERE id = %s", (doc_id,))


# ---------------------------------------------------------------------------
# Public lookup (client-facing) with lockout
# ---------------------------------------------------------------------------

_LOCKOUT_WINDOW = 300
_MAX_FAILED_ATTEMPTS = 20

_failed_attempts: dict = {}
_lockout_lock = threading.Lock()


def _prune_attempts(code):
    cutoff = time.time() - _LOCKOUT_WINDOW
    _failed_attempts[code] = [t for t in _failed_attempts.get(code, []) if t > cutoff]


def _is_locked_out(code):
    with _lockout_lock:
        _prune_attempts(code)
        return len(_failed_attempts.get(code, [])) >= _MAX_FAILED_ATTEMPTS


def _record_failure(code):
    with _lockout_lock:
        _failed_attempts.setdefault(code, []).append(time.time())


def _clear_failures(code):
    with _lockout_lock:
        _failed_attempts.pop(code, None)


def lookup_client(firm_slug, client_id_code, access_code):
    """Public client lookup scoped to a firm slug."""
    lock_key = f"{firm_slug}:{client_id_code}"
    if _is_locked_out(lock_key):
        return {"locked": True}

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT c.id, c.client_name, c.access_code_hash
                   FROM clients c
                   JOIN firms f ON f.id = c.firm_id
                   WHERE f.slug = %s AND c.client_id_code = %s""",
                (firm_slug, client_id_code),
            )
            row = cur.fetchone()
            if not row or not check_password_hash(row["access_code_hash"], access_code):
                _record_failure(lock_key)
                return None

            _clear_failures(lock_key)
            cur.execute(
                """SELECT name, description, notes, sort_order, status
                   FROM client_steps WHERE client_id = %s ORDER BY sort_order""",
                (row["id"],),
            )
            steps = cur.fetchall()
            return {"client_name": row["client_name"], "steps": steps}
