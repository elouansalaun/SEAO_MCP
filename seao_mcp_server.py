"""SEAO procurement-intelligence MCP server.

A read-only Model Context Protocol server over ``DATA/seao.db`` (Quebec SEAO
public-procurement open data, focused on security services). It gives an LLM
analyst a *hybrid* tool surface:

* a flexible core  -- ``describe_schema``, ``run_sql``, ``search_processes`` --
  for the long tail of ad-hoc questions, and
* curated analytical tools -- ``supplier_profile``, ``head_to_head``,
  ``price_benchmark``, ``incumbent``, ``buyer_profile``, ``market_overview`` --
  that bake in the three things a naive SQL query gets wrong on this data:

  1. **Supplier identity is the NEQ, not the name.** The same firm appears under
     many name spellings (e.g. Garda = 7 variants). Every supplier metric here is
     grouped on ``party.neq``, resolved from a name or NEQ via :func:`resolve_supplier`.
  2. **Win/loss is derived**, not stored: a bidder won a process iff its
     ``(ocid, party_id)`` appears in ``award_supplier`` for that process.
  3. **Direct (gre a gre) contracts have no bids** -- they are sole-sourced, so
     win-rate / margin analysis only applies to ``open`` and ``limited`` tenders.

All amounts are CAD. Run with:  ``python seao_mcp_server.py``  (stdio transport).
The DB path defaults to ``DATA/seao.db`` next to this file; override with the
``SEAO_DB_PATH`` environment variable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Database access (read-only)
# --------------------------------------------------------------------------- #
DB_PATH = Path(os.environ.get("SEAO_DB_PATH", Path(__file__).parent / "DATA" / "seao.db"))

# Verbs a read-only gateway must never run, matched as whole words.
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "create", "alter", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "commit", "begin",
)


def _unaccent(text: Optional[str]) -> Optional[str]:
    """Strip French diacritics so 'Quebec' matches 'Quebec'. Registered as a
    SQLite scalar function and used as ``unaccent(col) LIKE unaccent(?)``."""
    if text is None:
        return None
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def connect() -> sqlite3.Connection:
    """Open a fresh read-only connection with ``unaccent`` registered.

    A new connection per call keeps things thread-safe under FastMCP's worker
    pool; the database is tiny so the cost is negligible.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"SEAO database not found at {DB_PATH}. Set SEAO_DB_PATH or build it "
            f"with build_work_sqlite_db.py."
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("unaccent", 1, _unaccent, deterministic=True)
    return conn


def _query(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Run a SELECT under a 5s watchdog (``conn.interrupt`` from a timer)."""
    timer = threading.Timer(5.0, conn.interrupt)
    timer.start()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        timer.cancel()


# --------------------------------------------------------------------------- #
# Reference data (codes the raw tables store as bare numbers)
# --------------------------------------------------------------------------- #
# SEAO delivery_area = Quebec administrative region code.
DELIVERY_AREAS = {
    "1": "Bas-Saint-Laurent", "2": "Saguenay-Lac-Saint-Jean",
    "3": "Capitale-Nationale", "4": "Mauricie", "5": "Estrie", "6": "Montreal",
    "7": "Outaouais", "8": "Abitibi-Temiscamingue", "9": "Cote-Nord",
    "10": "Nord-du-Quebec", "11": "Gaspesie-Iles-de-la-Madeleine",
    "12": "Chaudiere-Appalaches", "13": "Laval", "14": "Lanaudiere",
    "15": "Laurentides", "16": "Monteregie", "17": "Centre-du-Quebec",
}
# bid.value_unit (SEAO code) -- the unit the bid amount is expressed in.
VALUE_UNITS = {
    "1": "total / lump-sum amount (CAD)", "2": "total amount, variant (CAD)",
    "6": "total amount, variant (CAD)", "7": "hourly rate (CAD/h)",
    "8": "daily rate (CAD/day)", "9": "total amount, variant (CAD)",
    "10": "hourly rate (CAD/h)",
}
# process_tag values and what their presence means about the process lifecycle.
TAGS = {
    "tender": "an open call for tenders was published",
    "tenderUpdate": "the tender notice was amended",
    "tenderCancellation": "the tender was cancelled",
    "award": "a contract was awarded",
    "awardUpdate": "the award was amended",
    "contract": "a contract was signed/published",
    "contractUpdate": "the contract was amended",
    "contractTermination": "the contract was terminated / reached its end",
}
PROCUREMENT_METHODS = {
    "open": "public call for tenders (appel d'offres public) -- competitive, has bids",
    "limited": "invitation tender (appel d'offres sur invitation) -- competitive, has bids",
    "direct": "gre a gre (sole-source direct award) -- NOT competitive, no bids",
}


def _area_name(code: Optional[str]) -> str:
    """'6' -> '6 (Montreal)'; pass composites like '17,4' through with names."""
    if not code:
        return "n/a"
    parts = [p.strip() for p in str(code).split(",")]
    named = [f"{p} ({DELIVERY_AREAS[p]})" if p in DELIVERY_AREAS else p for p in parts]
    return ", ".join(named)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _m(x) -> str:
    """Format a CAD amount: 1234567.0 -> '1,234,567 $'."""
    if x is None:
        return "n/a"
    try:
        return f"{float(x):,.0f} $"
    except (TypeError, ValueError):
        return str(x)


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _stats(values) -> Optional[dict]:
    """min / max / avg / median / p25 / p75 / n over a list of numbers."""
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None

    def q(p):
        if n == 1:
            return vals[0]
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return vals[f] + (vals[c] - vals[f]) * (k - f)

    return {"n": n, "min": vals[0], "max": vals[-1], "avg": sum(vals) / n,
            "median": q(0.5), "p25": q(0.25), "p75": q(0.75)}


def _table(rows: list[sqlite3.Row], headers: Optional[list[str]] = None) -> str:
    """Render rows as a GitHub-flavoured markdown table."""
    if not rows:
        return "_(no rows)_"
    headers = headers or list(rows[0].keys())
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        cells = [("" if v is None else str(v)) for v in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _placeholders(items) -> str:
    return ",".join("?" for _ in items)


class _RowLike:
    """Make a plain dict quack like a sqlite3.Row for _table()."""
    def __init__(self, d):
        self._d = d
    def keys(self):
        return list(self._d.keys())
    def __iter__(self):
        return iter(self._d.values())


def _dict_rows(dicts) -> list:
    """Wrap a list of dicts so _table() can render them."""
    return [_RowLike(d) for d in dicts]


# --------------------------------------------------------------------------- #
# Supplier identity resolution (name OR NEQ -> the set of NEQs)
# --------------------------------------------------------------------------- #
class Supplier:
    """A resolved supplier: one or more NEQs + a canonical display name."""

    def __init__(self, query: str, neqs: list[str], display: str,
                 variants: list[str], note: str = ""):
        self.query = query
        self.neqs = neqs
        self.display = display
        self.variants = variants
        self.note = note

    @property
    def found(self) -> bool:
        return bool(self.neqs)


def resolve_supplier(conn: sqlite3.Connection, query: str) -> Supplier:
    """Resolve a free-text supplier name or a raw NEQ to its NEQ identity.

    Because one firm has many name spellings, every supplier metric keys on the
    NEQ. A name may legitimately map to >1 NEQ (e.g. distinct legal entities);
    all of them are returned and aggregated together.
    """
    q = (query or "").strip()
    if not q:
        return Supplier(query, [], "", [], "empty query")

    digits = q.replace(" ", "").replace("-", "")
    is_neq = digits.isdigit() and len(digits) >= 9

    if is_neq:
        rows = _query(conn, "SELECT DISTINCT neq FROM party WHERE neq = ?", (digits,))
        neqs = [r["neq"] for r in rows]
    else:
        rows = _query(conn, """
            SELECT neq FROM party
            WHERE neq IS NOT NULL AND unaccent(name) LIKE unaccent(?)
            GROUP BY neq
        """, (f"%{q}%",))
        neqs = [r["neq"] for r in rows]

    if not neqs:
        return Supplier(query, [], q, [], f"no supplier matched '{query}'")

    # Canonical name = most frequent award_supplier spelling for these NEQs,
    # falling back to the most frequent party name.
    ph = _placeholders(neqs)
    names = _query(conn, f"""
        SELECT s.supplier_name AS name, COUNT(*) AS n
        FROM award_supplier s JOIN party p
          ON p.ocid = s.ocid AND p.party_id = s.party_id
        WHERE p.neq IN ({ph}) AND s.supplier_name IS NOT NULL
        GROUP BY s.supplier_name ORDER BY n DESC
    """, neqs)
    if not names:
        names = _query(conn, f"""
            SELECT name, COUNT(*) AS n FROM party
            WHERE neq IN ({ph}) AND name IS NOT NULL
            GROUP BY name ORDER BY n DESC
        """, neqs)
    variants = [r["name"] for r in names]
    display = variants[0] if variants else q
    note = ""
    if len(neqs) > 1:
        note = f"matched {len(neqs)} NEQs (aggregated together): {', '.join(neqs)}"
    return Supplier(query, neqs, display, variants, note)


# --------------------------------------------------------------------------- #
# Scope helper: resolve a service / area / buyer filter to a set of ocids
# --------------------------------------------------------------------------- #
def _scope_ocids(conn, unspsc=None, text=None, delivery_area=None,
                 buyer=None) -> Optional[list[str]]:
    """Processes matching any combination of UNSPSC (prefix), full-text,
    delivery area (region code) and buyer name. Returns ``None`` when no filter
    is supplied, so callers can treat that as 'all processes' without building a
    giant IN clause; returns ``[]`` when filters match nothing."""
    clauses, params = [], []
    if unspsc:
        clauses.append(
            "(EXISTS (SELECT 1 FROM item i WHERE i.ocid = p.ocid "
            "         AND i.classification_id LIKE ?) "
            " OR EXISTS (SELECT 1 FROM item_additional_classification a "
            "            WHERE a.ocid = p.ocid AND a.code LIKE ?))")
        params += [f"{unspsc}%", f"{unspsc}%"]
    if text:
        phrase = '"' + str(text).replace('"', '""') + '"'
        clauses.append("p.ocid IN (SELECT ocid FROM process_fts WHERE process_fts MATCH ?)")
        params.append(phrase)
    if delivery_area:
        # comma-pad so '6' matches '6' and '17,6' but never '16'
        clauses.append("(',' || REPLACE(t.delivery_area,' ','') || ',') LIKE ('%,' || ? || ',%')")
        params.append(str(delivery_area).strip())
    if buyer:
        clauses.append("unaccent(p.buyer_name) LIKE unaccent(?)")
        params.append(f"%{buyer}%")
    if not clauses:
        return None  # no scope filter => caller treats this as "all processes"
    where = " WHERE " + " AND ".join(clauses)
    rows = _query(conn, f"""
        SELECT DISTINCT p.ocid FROM process p JOIN tender t ON t.ocid = p.ocid{where}
    """, params)
    return [r["ocid"] for r in rows]


# =========================================================================== #
# MCP server
# =========================================================================== #
INSTRUCTIONS = """\
SEAO Quebec public-procurement intelligence (security-services subset, amounts in CAD).

Read this before composing supplier or competitor queries:
- SUPPLIER IDENTITY = NEQ, never the name. One firm has many name spellings, so
  group/join on party.neq. The curated tools (supplier_profile, head_to_head,
  price_benchmark, incumbent, buyer_profile) already resolve names -> NEQ for you;
  prefer them over hand-written SQL for any supplier/competitor metric.
- WIN/LOSS is derived: a bidder WON a process iff its (ocid, party_id) is in
  award_supplier. Bids live in table `bid` (link bid.party_id -> party to get NEQ).
- DIRECT 'gre a gre' awards (tender.procurement_method='direct') have NO bids;
  only 'open' and 'limited' tenders are competitive. Win-rate/margin = open+limited only.
- delivery_area is a Quebec region code (6 = Montreal). Use describe_schema for the map.
- Coverage skews to 2021-2025; ~1,785 processes. Treat figures as indicative, not exhaustive.
Use describe_schema first if you are unsure of a column; use run_sql for the long tail.
"""

mcp = FastMCP("seao", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Flexible core
# --------------------------------------------------------------------------- #
@mcp.tool()
def describe_schema(table: Optional[str] = None) -> str:
    """Data dictionary for the SEAO database: tables, columns, and the code
    lookups (delivery-area names, bid value-unit meanings, process tags,
    procurement methods) needed to write correct SQL.

    Call with no argument for the overview + reference codes + modelling rules.
    Pass a table/view name (e.g. 'award', 'v_contracts') for its columns and notes.
    Always consult this before writing run_sql for an unfamiliar column.
    """
    with connect() as conn:
        if table:
            cols = _query(conn, f"PRAGMA table_info({table})")
            if not cols:
                avail = _query(conn,
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'process_fts_%' "
                    "ORDER BY name")
                return (f"No table/view named '{table}'. Available: "
                        + ", ".join(r["name"] for r in avail))
            docs = {r["column_name"]: r["description"] for r in _query(conn,
                "SELECT column_name, description FROM schema_doc WHERE table_name = ?",
                (table,))}
            tbl_doc = docs.get(None, "")
            lines = [f"## {table}", tbl_doc, "", "| column | type | notes |", "| --- | --- | --- |"]
            for c in cols:
                lines.append(f"| {c['name']} | {c['type']} | {docs.get(c['name'], '')} |")
            return "\n".join(lines)

        tables = _query(conn,
            "SELECT table_name, description FROM schema_doc WHERE column_name IS NULL "
            "ORDER BY table_name")
        out = ["# SEAO database -- overview", "",
               "**Amounts CAD. Supplier identity = party.neq (not name). "
               "A bidder won iff (ocid, party_id) in award_supplier. "
               "'direct' = gre a gre, no bids.**", "",
               "## Tables & views (call describe_schema('<name>') for columns)"]
        for t in tables:
            out.append(f"- **{t['table_name']}** -- {t['description']}")
        out += ["", "## Delivery areas (tender.delivery_area = Quebec region code)"]
        out.append(", ".join(f"{k}={v}" for k, v in DELIVERY_AREAS.items()))
        out += ["", "## bid.value_unit codes"]
        out += [f"- {k} = {v}" for k, v in VALUE_UNITS.items()]
        out += ["", "## process_tag values (lifecycle)"]
        out += [f"- {k} = {v}" for k, v in TAGS.items()]
        out += ["", "## tender.procurement_method"]
        out += [f"- {k} = {v}" for k, v in PROCUREMENT_METHODS.items()]
        return "\n".join(out)


@mcp.tool()
def run_sql(sql: str, limit: int = 200) -> str:
    """Run an arbitrary read-only SELECT (or WITH ... SELECT) against the SEAO
    database and return the rows as a markdown table. The escape hatch for any
    question the curated tools don't cover.

    Only a single SELECT/WITH statement is allowed; any write/DDL/PRAGMA is
    rejected and the connection is opened read-only. A LIMIT is auto-appended if
    you omit one. Reminder: group supplier metrics on party.neq, and derive
    win/loss from award_supplier membership (see describe_schema).
    """
    s = sql.strip().rstrip(";").strip()
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "ERROR: only SELECT / WITH queries are allowed."
    if ";" in s:
        return "ERROR: only a single statement is allowed (remove ';')."
    import re
    for verb in _FORBIDDEN:
        if re.search(rf"\b{verb}\b", low):
            return f"ERROR: '{verb}' is not allowed in this read-only gateway."
    limit = max(1, min(int(limit), 2000))
    if not re.search(r"\blimit\b", low):
        s = f"{s}\nLIMIT {limit}"
    try:
        with connect() as conn:
            rows = _query(conn, s)
    except sqlite3.Error as e:
        return f"SQL error: {e}"
    note = f"\n\n_{len(rows)} row(s)" + (f"; capped at {limit}_" if len(rows) >= limit else "_")
    return _table(rows) + note


@mcp.tool()
def search_processes(text: Optional[str] = None, unspsc: Optional[str] = None,
                     buyer: Optional[str] = None, delivery_area: Optional[str] = None,
                     status: Optional[str] = None, procurement_method: Optional[str] = None,
                     limit: int = 25) -> str:
    """Find contracting processes (tenders) matching any mix of filters -- the
    entry point for 'which tenders are relevant here?'.

    Args:
        text: accent-insensitive full-text over title / buyer / supplier / item
            descriptions (e.g. 'gardiennage', 'agence de securite').
        unspsc: UNSPSC code or prefix (e.g. '90152100', or '9212' for the family).
        buyer: buyer-name substring (e.g. 'Marguerite-Bourgeoys').
        delivery_area: Quebec region code (e.g. '6' for Montreal).
        status: tender status -- 'complete', 'active', or 'cancelled'.
        procurement_method: 'open', 'limited', or 'direct' (gre a gre).
        limit: max rows (default 25).

    Returns one row per process with buyer, method, status, #tenderers, the
    awarded value and winning supplier(s).
    """
    clauses, params = [], []
    if text:
        phrase = '"' + str(text).replace('"', '""') + '"'
        clauses.append("t.ocid IN (SELECT ocid FROM process_fts WHERE process_fts MATCH ?)")
        params.append(phrase)
    if unspsc:
        clauses.append("EXISTS (SELECT 1 FROM item i WHERE i.ocid = t.ocid AND i.classification_id LIKE ?)")
        params.append(f"{unspsc}%")
    if buyer:
        clauses.append("unaccent(p.buyer_name) LIKE unaccent(?)")
        params.append(f"%{buyer}%")
    if delivery_area:
        clauses.append("(',' || REPLACE(t.delivery_area,' ','') || ',') LIKE ('%,' || ? || ',%')")
        params.append(str(delivery_area).strip())
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    if procurement_method:
        clauses.append("t.procurement_method = ?")
        params.append(procurement_method)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = _query(conn, f"""
            SELECT t.ocid,
                   substr(t.title,1,70) AS title,
                   p.buyer_name AS buyer,
                   t.procurement_method AS method,
                   t.status,
                   t.delivery_area AS area,
                   t.number_of_tenderers AS bidders,
                   (SELECT SUM(a.value_amount) FROM award a WHERE a.ocid = t.ocid) AS awarded,
                   (SELECT group_concat(DISTINCT s.supplier_name)
                      FROM award_supplier s WHERE s.ocid = t.ocid) AS winner
            FROM tender t JOIN process p ON p.ocid = t.ocid{where}
            ORDER BY t.start_date DESC
            LIMIT ?
        """, params + [limit])
    if not rows:
        return "No processes matched those filters."
    out = [f"Found {len(rows)} process(es):", ""]
    disp = []
    for r in rows:
        d = dict(r)
        d["area"] = _area_name(r["area"])
        d["awarded"] = _m(r["awarded"])
        disp.append(d)
    # render manually to apply formatting
    headers = ["ocid", "title", "buyer", "method", "status", "area", "bidders", "awarded", "winner"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for d in disp:
        lines.append("| " + " | ".join(str(d.get(h) if d.get(h) is not None else "") for h in headers) + " |")
    return "\n".join(out + lines)


# --------------------------------------------------------------------------- #
# Curated analytical tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def supplier_profile(supplier: str) -> str:
    """Full competitive profile of one supplier, resolved by NEQ (handles the
    many name spellings). Answers 'win rate', 'total won', 'where do they play',
    'who are their rivals'.

    Args:
        supplier: a supplier name (any spelling) or a raw NEQ.

    Reports: awards won and total value (all methods); competitive win rate over
    open+limited tenders they bid on; procurement-method split; top buyers; the
    delivery areas they target; and their most frequent rival bidders with the
    head-to-head record. Win-rate excludes 'direct' gre a gre (those have no bids).
    """
    with connect() as conn:
        sup = resolve_supplier(conn, supplier)
        if not sup.found:
            return f"No supplier matched '{supplier}'. Try search_processes(text=...) to find the exact name."
        ph = _placeholders(sup.neqs)
        neqs = sup.neqs

        # Awards won (all methods) + value
        won = _query(conn, f"""
            SELECT COUNT(*) AS awards, SUM(a.value_amount) AS total,
                   COUNT(DISTINCT s.ocid) AS processes
            FROM award_supplier s
            JOIN party p ON p.ocid = s.ocid AND p.party_id = s.party_id
            JOIN award a ON a.ocid = s.ocid AND a.award_id = s.award_id
            WHERE p.neq IN ({ph})
        """, neqs)[0]

        method_split = _query(conn, f"""
            SELECT t.procurement_method AS method, COUNT(*) AS awards,
                   SUM(a.value_amount) AS total
            FROM award_supplier s
            JOIN party p ON p.ocid = s.ocid AND p.party_id = s.party_id
            JOIN award a ON a.ocid = s.ocid AND a.award_id = s.award_id
            JOIN tender t ON t.ocid = s.ocid
            WHERE p.neq IN ({ph}) GROUP BY t.procurement_method ORDER BY awards DESC
        """, neqs)

        # Competitive win rate: of the open/limited processes they BID on, how
        # many did they win at least one award in.
        comp = _query(conn, f"""
            WITH bid_procs AS (
                SELECT DISTINCT b.ocid FROM bid b
                JOIN party p ON p.ocid = b.ocid AND p.party_id = b.party_id
                JOIN tender t ON t.ocid = b.ocid
                WHERE p.neq IN ({ph}) AND t.procurement_method IN ('open','limited')
            )
            SELECT
              (SELECT COUNT(*) FROM bid_procs) AS bid_processes,
              (SELECT COUNT(*) FROM bid_procs bp WHERE EXISTS (
                  SELECT 1 FROM award_supplier s
                  JOIN party p ON p.ocid = s.ocid AND p.party_id = s.party_id
                  WHERE s.ocid = bp.ocid AND p.neq IN ({ph}))) AS won_processes
        """, neqs + neqs)[0]
        bp, wp = comp["bid_processes"], comp["won_processes"]
        win_rate = (wp / bp) if bp else None

        top_buyers = _query(conn, f"""
            SELECT pr.buyer_name AS buyer, COUNT(*) AS awards, SUM(a.value_amount) AS total
            FROM award_supplier s
            JOIN party p ON p.ocid = s.ocid AND p.party_id = s.party_id
            JOIN award a ON a.ocid = s.ocid AND a.award_id = s.award_id
            JOIN process pr ON pr.ocid = s.ocid
            WHERE p.neq IN ({ph}) GROUP BY pr.buyer_name ORDER BY awards DESC LIMIT 5
        """, neqs)

        areas = _query(conn, f"""
            SELECT t.delivery_area AS area, COUNT(*) AS awards
            FROM award_supplier s
            JOIN party p ON p.ocid = s.ocid AND p.party_id = s.party_id
            JOIN tender t ON t.ocid = s.ocid
            WHERE p.neq IN ({ph}) AND t.delivery_area IS NOT NULL
            GROUP BY t.delivery_area ORDER BY awards DESC LIMIT 5
        """, neqs)

        # Rivals: other NEQs that bid in the same processes this supplier bid on.
        rivals = _query(conn, f"""
            WITH my_procs AS (
                SELECT DISTINCT b.ocid FROM bid b
                JOIN party p ON p.ocid = b.ocid AND p.party_id = b.party_id
                WHERE p.neq IN ({ph})
            )
            SELECT p2.neq AS neq,
                   COUNT(DISTINCT b2.ocid) AS met,
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM award_supplier s2
                       JOIN party ps ON ps.ocid = s2.ocid AND ps.party_id = s2.party_id
                       WHERE s2.ocid = b2.ocid AND ps.neq = p2.neq) THEN b2.ocid END) AS rival_wins
            FROM bid b2
            JOIN party p2 ON p2.ocid = b2.ocid AND p2.party_id = b2.party_id
            WHERE b2.ocid IN (SELECT ocid FROM my_procs)
              AND p2.neq NOT IN ({ph}) AND p2.neq IS NOT NULL
            GROUP BY p2.neq ORDER BY met DESC LIMIT 5
        """, neqs + neqs)

    out = [f"# Supplier profile: {sup.display}",
           f"NEQ: {', '.join(sup.neqs)}" + (f"  \n_{sup.note}_" if sup.note else "")]
    if len(sup.variants) > 1:
        out.append(f"_Name spellings on file: {'; '.join(sup.variants[:6])}_")
    out += ["",
            f"- **Awards won (all methods):** {won['awards']} across {won['processes']} processes, "
            f"total **{_m(won['total'])}**",
            f"- **Competitive win rate (open+limited):** "
            + (f"**{_pct(win_rate)}** ({wp} won / {bp} bid on)" if bp else "no competitive bids on record"),
            "", "**By procurement method:**"]
    msrows = _dict_rows([{"method": m["method"], "awards": m["awards"],
                          "total": _m(m["total"])} for m in method_split])
    out.append(_table(msrows) if msrows else "_none_")
    out += ["", "**Top buyers:**"]
    tbrows = _dict_rows([{"buyer": b["buyer"], "awards": b["awards"],
                          "total": _m(b["total"])} for b in top_buyers])
    out.append(_table(tbrows) if tbrows else "_none_")
    if areas:
        out += ["", "**Delivery areas targeted:**"]
        out += [f"- {_area_name(a['area'])}: {a['awards']} award(s)" for a in areas]
    if rivals:
        out += ["", "**Most frequent rival bidders** (tenders co-bid / of those the rival won >=1 award):"]
        with connect() as conn:
            for rv in rivals:
                rn = resolve_supplier(conn, rv["neq"])
                out.append(f"- {rn.display} (NEQ {rv['neq']}): co-bid {rv['met']}x, rival won {rv['rival_wins']}")
    out.append("\n_Win-rate counts a process as won if the supplier took >=1 award; "
               "'direct' gre a gre awards have no bids and are excluded from win-rate._")
    return "\n".join(out)


@mcp.tool()
def head_to_head(supplier_a: str, supplier_b: str) -> str:
    """Direct rivalry record between two suppliers: how often they bid on the
    same tender and who won each time. Both are resolved by NEQ.

    Args:
        supplier_a: first supplier name or NEQ.
        supplier_b: second supplier name or NEQ.

    Returns times-met (both submitted a bid on the same process), how often each
    won, how often a third party won, and a few example processes.
    """
    with connect() as conn:
        a = resolve_supplier(conn, supplier_a)
        b = resolve_supplier(conn, supplier_b)
        if not a.found:
            return f"No supplier matched '{supplier_a}'."
        if not b.found:
            return f"No supplier matched '{supplier_b}'."
        pha, phb = _placeholders(a.neqs), _placeholders(b.neqs)

        shared = _query(conn, f"""
            SELECT DISTINCT ba.ocid FROM bid ba
            JOIN party pa ON pa.ocid = ba.ocid AND pa.party_id = ba.party_id
            WHERE pa.neq IN ({pha})
              AND ba.ocid IN (
                SELECT bb.ocid FROM bid bb
                JOIN party pb ON pb.ocid = bb.ocid AND pb.party_id = bb.party_id
                WHERE pb.neq IN ({phb}))
        """, a.neqs + b.neqs)
        ocids = [r["ocid"] for r in shared]
        if not ocids:
            return (f"**{a.display}** and **{b.display}** have no tenders where both "
                    f"submitted a bid on record.")

        only_a = only_b = both = neither = 0
        examples = []
        for ocid in ocids:
            won_a = bool(_query(conn, f"""
                SELECT 1 FROM award_supplier s JOIN party p
                  ON p.ocid = s.ocid AND p.party_id = s.party_id
                WHERE s.ocid = ? AND p.neq IN ({pha}) LIMIT 1""", [ocid] + a.neqs))
            won_b = bool(_query(conn, f"""
                SELECT 1 FROM award_supplier s JOIN party p
                  ON p.ocid = s.ocid AND p.party_id = s.party_id
                WHERE s.ocid = ? AND p.neq IN ({phb}) LIMIT 1""", [ocid] + b.neqs))
            if won_a and won_b:
                both += 1; winner = "both (different lots)"
            elif won_a:
                only_a += 1; winner = a.display
            elif won_b:
                only_b += 1; winner = b.display
            else:
                neither += 1; winner = "third party / no award"
            if len(examples) < 5:
                title = _query(conn, "SELECT title FROM tender WHERE ocid = ?", (ocid,))
                examples.append((ocid, (title[0]["title"] or "")[:55], winner))

    out = [f"# Head-to-head: {a.display}  vs  {b.display}",
           f"NEQ {','.join(a.neqs)}  vs  NEQ {','.join(b.neqs)}", "",
           f"- **Times both bid the same tender:** {len(ocids)}",
           f"- **{a.display} won (only):** {only_a}",
           f"- **{b.display} won (only):** {only_b}",
           f"- **Both won (different lots):** {both}",
           f"- **A third party won (or no award):** {neither}", "",
           "**Examples:**", "", "| ocid | tender | winner |", "| --- | --- | --- |"]
    for ocid, title, winner in examples:
        out.append(f"| {ocid} | {title} | {winner} |")
    return "\n".join(out)


@mcp.tool()
def price_benchmark(unspsc: Optional[str] = None, text: Optional[str] = None,
                    delivery_area: Optional[str] = None, value_unit: str = "1") -> str:
    """Historical winning-price benchmark for a service, optionally by region --
    'what do contracts like this go for, and by how much does the winner beat
    the runner-up?'.

    Args:
        unspsc: UNSPSC code/prefix of the service (e.g. '90152100' guarding).
        text: OR a full-text service description (e.g. 'gardiennage').
        delivery_area: optional Quebec region code (e.g. '6' Montreal).
        value_unit: bid unit to benchmark, default '1' = total lump-sum CAD.
            Use '7' or '10' for hourly rates. See describe_schema for the codes.

    Reports the distribution of awarded values (all methods) and, for competitive
    tenders only, the winning bid distribution and the winner-vs-next-lowest-rival
    margin. Mixing units is avoided by filtering bids to one value_unit.
    """
    if not (unspsc or text or delivery_area):
        return "Provide at least one of: unspsc, text, or delivery_area."
    with connect() as conn:
        ocids = _scope_ocids(conn, unspsc=unspsc, text=text, delivery_area=delivery_area)
        if not ocids:
            return "No processes matched that scope."
        ph = _placeholders(ocids)

        award_vals = [r["value_amount"] for r in _query(conn, f"""
            SELECT a.value_amount FROM award a
            WHERE a.ocid IN ({ph}) AND a.status = 'active' AND a.value_amount IS NOT NULL
        """, ocids)]

        # Winning bids of the chosen unit (competitive only).
        win_bids = [r["value_amount"] for r in _query(conn, f"""
            SELECT b.value_amount FROM bid b
            JOIN award_supplier s ON s.ocid = b.ocid AND s.party_id = b.party_id
            WHERE b.ocid IN ({ph}) AND b.value_unit = ? AND b.value_amount IS NOT NULL
        """, ocids + [value_unit])]

        # Winner vs next-lowest competing bid, per process (same unit).
        margins = []
        for ocid in ocids:
            bids = _query(conn, f"""
                SELECT b.value_amount AS amt,
                       EXISTS (SELECT 1 FROM award_supplier s
                               WHERE s.ocid = b.ocid AND s.party_id = b.party_id) AS won
                FROM bid b
                WHERE b.ocid = ? AND b.value_unit = ? AND b.value_amount IS NOT NULL
            """, (ocid, value_unit))
            winners = [b["amt"] for b in bids if b["won"]]
            losers = [b["amt"] for b in bids if not b["won"]]
            if winners and losers:
                w = min(winners)
                nxt = min(losers)
                if w:
                    margins.append((nxt - w) / w)

    out = [f"# Price benchmark"]
    scope = []
    if unspsc: scope.append(f"UNSPSC {unspsc}*")
    if text: scope.append(f"text '{text}'")
    if delivery_area: scope.append(f"area {_area_name(delivery_area)}")
    out.append("Scope: " + ", ".join(scope) + f"  ({len(ocids)} processes)\n")

    av = _stats(award_vals)
    if av:
        out += ["**Awarded value (all methods, award.value_amount):**",
                f"- n={av['n']}  avg **{_m(av['avg'])}**  median **{_m(av['median'])}**",
                f"- range {_m(av['min'])} -- {_m(av['max'])}  (p25 {_m(av['p25'])}, p75 {_m(av['p75'])})", ""]
    else:
        out += ["_No awarded values on record for this scope._", ""]

    wb = _stats(win_bids)
    unit_label = VALUE_UNITS.get(value_unit, value_unit)
    if wb:
        out += [f"**Winning bid (competitive tenders, unit '{value_unit}' = {unit_label}):**",
                f"- n={wb['n']}  avg **{_m(wb['avg'])}**  median **{_m(wb['median'])}**",
                f"- range {_m(wb['min'])} -- {_m(wb['max'])}", ""]
    else:
        out += [f"_No winning bids of unit '{value_unit}' for this scope "
                f"(try a different value_unit, or these may be direct awards)._", ""]

    if margins:
        ms = _stats(margins)
        out += ["**Winner vs next-lowest rival bid** (same unit; +ve = winner priced "
                "above the cheapest rival, i.e. won on non-price criteria):",
                f"- n={ms['n']} competitive processes  median **{ms['median'] * 100:+.1f}%**  "
                f"avg **{ms['avg'] * 100:+.1f}%**"]
    else:
        out += ["_Not enough head-to-head competitive bids to measure winner-vs-runner-up margin._"]
    return "\n".join(out)


@mcp.tool()
def incumbent(buyer: str, service: Optional[str] = None,
              unspsc: Optional[str] = None) -> str:
    """Who currently holds (and previously held) a buyer's contract for a given
    service, with the awarded and contract values -- tactical context before
    bidding on a renewal.

    Args:
        buyer: buyer-name substring (e.g. 'Marguerite-Bourgeoys').
        service: optional full-text service filter (e.g. 'securite', 'gardiennage').
        unspsc: optional UNSPSC code/prefix to pin the service precisely.

    Returns awards for that buyer (most recent first): supplier, awarded value,
    signed contract value, contract end, and status. The top row is the current
    incumbent.
    """
    with connect() as conn:
        ocids = _scope_ocids(conn, buyer=buyer, text=service, unspsc=unspsc)
        if not ocids:
            return (f"No processes found for buyer matching '{buyer}'"
                    + (f" + service '{service or unspsc}'" if (service or unspsc) else "")
                    + ". Try search_processes to find the exact buyer name.")
        ph = _placeholders(ocids)
        rows = _query(conn, f"""
            SELECT a.date AS award_date,
                   pr.buyer_name AS buyer,
                   substr(t.title,1,55) AS title,
                   (SELECT group_concat(DISTINCT s.supplier_name)
                      FROM award_supplier s WHERE s.ocid = a.ocid AND s.award_id = a.award_id) AS supplier,
                   a.value_amount AS awarded,
                   a.value_total_amount AS awarded_total,
                   (SELECT SUM(c.value_amount) FROM contract c WHERE c.ocid = a.ocid) AS contract_value,
                   (SELECT MAX(c.period_end) FROM contract c WHERE c.ocid = a.ocid) AS contract_end,
                   t.procurement_method AS method
            FROM award a
            JOIN process pr ON pr.ocid = a.ocid
            JOIN tender t ON t.ocid = a.ocid
            WHERE a.ocid IN ({ph})
            ORDER BY a.date DESC
        """, ocids)
    if not rows:
        return f"Processes found for '{buyer}' but no awards recorded."
    out = [f"# Incumbent history -- buyer matching '{buyer}'"
           + (f", service '{service or unspsc}'" if (service or unspsc) else ""),
           f"_{len(rows)} award(s); most recent first -- top row is the current incumbent._", "",
           "| award date | supplier | awarded | award total | contract $ | contract end | method | tender |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        out.append("| " + " | ".join([
            (r["award_date"] or "")[:10],
            r["supplier"] or "?",
            _m(r["awarded"]), _m(r["awarded_total"]),
            _m(r["contract_value"]), (r["contract_end"] or "")[:10],
            r["method"] or "", r["title"] or "",
        ]) + " |")
    return "\n".join(out)


@mcp.tool()
def buyer_profile(buyer: str) -> str:
    """Procurement behaviour of a buyer: open vs gre a gre mix, total spend,
    supplier loyalty/rotation, and activity over time.

    Args:
        buyer: buyer-name substring (e.g. 'Surete du Quebec').

    Reports the procurement-method split, total awarded, distinct-supplier count,
    the top suppliers by value with the top supplier's share (a loyalty signal),
    and awarded volume by year.
    """
    with connect() as conn:
        names = _query(conn, """
            SELECT DISTINCT buyer_name FROM process
            WHERE buyer_name IS NOT NULL AND unaccent(buyer_name) LIKE unaccent(?)
        """, (f"%{buyer}%",))
        if not names:
            return f"No buyer matched '{buyer}'. Try search_processes to find the name."
        buyer_names = [n["buyer_name"] for n in names]
        ph = _placeholders(buyer_names)

        method = _query(conn, f"""
            SELECT t.procurement_method AS method, COUNT(*) AS processes,
                   SUM((SELECT SUM(a.value_amount) FROM award a WHERE a.ocid = t.ocid)) AS total
            FROM tender t JOIN process p ON p.ocid = t.ocid
            WHERE p.buyer_name IN ({ph}) GROUP BY t.procurement_method ORDER BY processes DESC
        """, buyer_names)

        totals = _query(conn, f"""
            SELECT COUNT(DISTINCT p.ocid) AS processes,
                   SUM(a.value_amount) AS total,
                   MIN(a.date) AS first_award, MAX(a.date) AS last_award
            FROM process p LEFT JOIN award a ON a.ocid = p.ocid
            WHERE p.buyer_name IN ({ph})
        """, buyer_names)[0]

        suppliers = _query(conn, f"""
            SELECT COALESCE(pa.neq, s.supplier_name) AS supplier_key,
                   MIN(s.supplier_name) AS name,
                   COUNT(*) AS awards, SUM(a.value_amount) AS total
            FROM award_supplier s
            JOIN process p ON p.ocid = s.ocid
            JOIN award a ON a.ocid = s.ocid AND a.award_id = s.award_id
            LEFT JOIN party pa ON pa.ocid = s.ocid AND pa.party_id = s.party_id
            WHERE p.buyer_name IN ({ph})
            GROUP BY supplier_key ORDER BY total DESC NULLS LAST
        """, buyer_names)

        by_year = _query(conn, f"""
            SELECT substr(a.date,1,4) AS year, COUNT(*) AS awards, SUM(a.value_amount) AS total
            FROM award a JOIN process p ON p.ocid = a.ocid
            WHERE p.buyer_name IN ({ph}) AND a.date IS NOT NULL
            GROUP BY year ORDER BY year
        """, buyer_names)

    grand_total = sum((s["total"] or 0) for s in suppliers)
    top_share = (suppliers[0]["total"] or 0) / grand_total if (suppliers and grand_total) else None
    label = buyer_names[0] + (f" (+{len(buyer_names) - 1} name variants)" if len(buyer_names) > 1 else "")
    out = [f"# Buyer profile: {label}", "",
           f"- **Processes:** {totals['processes']}   **Total awarded:** {_m(totals['total'])}",
           f"- **Award window:** {(totals['first_award'] or '?')[:10]} -> {(totals['last_award'] or '?')[:10]}",
           f"- **Distinct suppliers:** {len(suppliers)}"
           + (f"   **Top supplier share:** {_pct(top_share)} "
              + ("(concentrated -> loyal)" if (top_share or 0) >= 0.5 else "(spread -> rotates)")
              if top_share is not None else ""),
           "", "**Procurement method mix:**"]
    mrows = _dict_rows([{"method": m["method"], "processes": m["processes"],
                         "total": _m(m["total"])} for m in method])
    out.append(_table(mrows) if mrows else "_none_")
    out += ["", "**Top suppliers (by value):**"]
    srows = _dict_rows([{"supplier": s["name"], "neq": s["supplier_key"],
                         "awards": s["awards"], "total": _m(s["total"])} for s in suppliers[:8]])
    out.append(_table(srows) if suppliers else "_none_")
    if by_year:
        out += ["", "**Awarded by year:**"]
        out += [f"- {y['year']}: {y['awards']} award(s), {_m(y['total'])}" for y in by_year]
    return "\n".join(out)


@mcp.tool()
def market_overview(year: Optional[str] = None, unspsc: Optional[str] = None,
                    text: Optional[str] = None, top_n: int = 3) -> str:
    """Macro view of the market (or a slice of it): total value by year, supplier
    concentration (top-N share), procurement-method trend, and geographic spread.

    Args:
        year: optional 4-digit year to focus on (e.g. '2021').
        unspsc: optional UNSPSC code/prefix to scope to a service.
        text: optional full-text scope (e.g. 'gardiennage').
        top_n: how many top firms for the concentration metric (default 3).

    Reports the total addressable value per year, the market share held by the
    top-N suppliers (consolidation), method mix, and the busiest delivery areas.
    """
    with connect() as conn:
        ocids = _scope_ocids(conn, unspsc=unspsc, text=text)
        ph = _placeholders(ocids) if ocids else None
        if ocids is not None and not ocids:
            return "No processes matched that scope."
        scope_clause = f"a.ocid IN ({ph})" if ph else "1=1"
        year_clause = " AND substr(a.date,1,4) = ?" if year else ""
        yparams = [year] if year else []

        by_year = _query(conn, f"""
            SELECT substr(a.date,1,4) AS year, COUNT(*) AS awards, SUM(a.value_amount) AS total
            FROM award a WHERE {scope_clause} AND a.date IS NOT NULL
            GROUP BY year ORDER BY year
        """, (ocids or []))

        top = _query(conn, f"""
            SELECT COALESCE(pa.neq, s.supplier_name) AS supplier_key,
                   MIN(s.supplier_name) AS name, SUM(a.value_amount) AS total
            FROM award_supplier s
            JOIN award a ON a.ocid = s.ocid AND a.award_id = s.award_id
            LEFT JOIN party pa ON pa.ocid = s.ocid AND pa.party_id = s.party_id
            WHERE {scope_clause}{year_clause}
            GROUP BY supplier_key ORDER BY total DESC NULLS LAST
        """, (ocids or []) + yparams)

        methods = _query(conn, f"""
            SELECT t.procurement_method AS method, COUNT(*) AS processes
            FROM tender t WHERE {scope_clause.replace('a.ocid', 't.ocid')}
            {('AND t.ocid IN (SELECT ocid FROM award WHERE substr(date,1,4)=?)' if year else '')}
            GROUP BY method ORDER BY processes DESC
        """, (ocids or []) + yparams)

        areas = _query(conn, f"""
            SELECT t.delivery_area AS area, COUNT(*) AS processes
            FROM tender t WHERE {scope_clause.replace('a.ocid', 't.ocid')}
              AND t.delivery_area IS NOT NULL
            GROUP BY area ORDER BY processes DESC LIMIT 6
        """, (ocids or []))

    grand = sum((t["total"] or 0) for t in top)
    topn = top[:max(1, top_n)]
    topn_total = sum((t["total"] or 0) for t in topn)
    share = topn_total / grand if grand else None

    out = ["# Market overview"]
    sc = []
    if unspsc: sc.append(f"UNSPSC {unspsc}*")
    if text: sc.append(f"'{text}'")
    if year: sc.append(f"year {year}")
    out.append("Scope: " + (", ".join(sc) if sc else "all security-services processes") + "\n")

    if by_year:
        out += ["**Total awarded value by year (TAM):**"]
        out += [f"- {y['year']}: {_m(y['total'])}  ({y['awards']} awards)" for y in by_year if y["year"]]
        out.append("")
    out += [f"**Market concentration:** top {len(topn)} firms hold "
            f"**{_pct(share)}** of awarded value ({_m(topn_total)} of {_m(grand)}).", ""]
    out += ["**Top firms:**"]
    for t in topn:
        out.append(f"- {t['name']} (NEQ {t['supplier_key']}): {_m(t['total'])}")
    if methods:
        out += ["", "**Procurement method mix:**"]
        out += [f"- {m['method']}: {m['processes']} processes" for m in methods]
    if areas:
        out += ["", "**Busiest delivery areas:**"]
        out += [f"- {_area_name(a['area'])}: {a['processes']} processes" for a in areas]
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
