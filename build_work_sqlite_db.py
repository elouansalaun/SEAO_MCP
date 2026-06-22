"""Transform the OCDS ``filtered_records.json`` into a query-ready SQLite database.

The SEAO open data is published as Open Contracting Data Standard (OCDS) releases
with the ``ocds_lots_extension``. That nested JSON is great for storage but poor
for querying. This script normalizes it into a relational schema and adds an FTS5
full-text index, convenience views and a machine-readable data dictionary so the
database can be queried directly by an LLM through a generic SQLite MCP server.

Usage:
    python build_work_sqlite_db.py [input.json] [output.db]

Defaults: input = DATA/filtered_records.json, output = DATA/seao.db
"""

import argparse
import json
import sqlite3
from pathlib import Path


# --------------------------------------------------------------------------- #
# Schema (DDL)
# --------------------------------------------------------------------------- #
# All amounts are in CAD. Dates are kept as raw ISO 8601 strings (lexically
# sortable, usable with SQLite date()/substr()). Party ids (e.g. "OP-387",
# "FO-...") are release-scoped, so every party-keyed table includes the ocid.

SCHEMA = """
PRAGMA foreign_keys = OFF;

DROP VIEW  IF EXISTS v_buyer_stats;
DROP VIEW  IF EXISTS v_supplier_stats;
DROP VIEW  IF EXISTS v_tenders;
DROP VIEW  IF EXISTS v_awards;
DROP VIEW  IF EXISTS v_contracts;
DROP TABLE IF EXISTS process_fts;
DROP TABLE IF EXISTS schema_doc;
DROP TABLE IF EXISTS related_process;
DROP TABLE IF EXISTS contract_transaction;
DROP TABLE IF EXISTS contract_amendment;
DROP TABLE IF EXISTS contract;
DROP TABLE IF EXISTS award_supplier;
DROP TABLE IF EXISTS award;
DROP TABLE IF EXISTS bid_related_lot;
DROP TABLE IF EXISTS bid;
DROP TABLE IF EXISTS lot;
DROP TABLE IF EXISTS item_additional_classification;
DROP TABLE IF EXISTS item;
DROP TABLE IF EXISTS document;
DROP TABLE IF EXISTS tender_category;
DROP TABLE IF EXISTS tender;
DROP TABLE IF EXISTS party_role;
DROP TABLE IF EXISTS party;
DROP TABLE IF EXISTS process_tag;
DROP TABLE IF EXISTS process;

-- One row per OCDS contracting process (ocid is unique in the source).
CREATE TABLE process (
    ocid            TEXT PRIMARY KEY,
    release_id      TEXT,
    release_date    TEXT,
    language        TEXT,
    initiation_type TEXT,
    buyer_id        TEXT,
    buyer_name      TEXT
);

CREATE TABLE process_tag (
    ocid TEXT NOT NULL REFERENCES process(ocid),
    tag  TEXT NOT NULL,
    PRIMARY KEY (ocid, tag)
);

-- Organisations involved in a process. party_id is release-scoped: join on
-- (ocid, party_id). neq + name are the stable cross-process supplier identity.
CREATE TABLE party (
    ocid           TEXT NOT NULL REFERENCES process(ocid),
    party_id       TEXT NOT NULL,
    name           TEXT,
    neq            TEXT,
    municipal      TEXT,
    street_address TEXT,
    locality       TEXT,
    region         TEXT,
    country_name   TEXT,
    postal_code    TEXT,
    PRIMARY KEY (ocid, party_id)
);

CREATE TABLE party_role (
    ocid     TEXT NOT NULL,
    party_id TEXT NOT NULL,
    role     TEXT NOT NULL,
    PRIMARY KEY (ocid, party_id, role)
);

CREATE TABLE tender (
    ocid                         TEXT PRIMARY KEY REFERENCES process(ocid),
    tender_id                    TEXT,
    title                        TEXT,
    status                       TEXT,
    procurement_method           TEXT,
    procurement_method_details   TEXT,
    procurement_method_rationale TEXT,
    main_procurement_category    TEXT,
    delivery_area                TEXT,
    number_of_tenderers          INTEGER,
    procuring_entity_id          TEXT,
    procuring_entity_name        TEXT,
    start_date                   TEXT,
    end_date                     TEXT,
    duration_days                INTEGER,
    value_amount                 REAL,
    min_value_amount             REAL
);

CREATE TABLE tender_category (
    ocid     TEXT NOT NULL REFERENCES process(ocid),
    category TEXT NOT NULL,
    PRIMARY KEY (ocid, category)
);

CREATE TABLE document (
    ocid        TEXT NOT NULL REFERENCES process(ocid),
    document_id TEXT,
    url         TEXT
);

CREATE TABLE item (
    ocid                       TEXT NOT NULL REFERENCES process(ocid),
    item_id                    TEXT NOT NULL,
    description                TEXT,
    classification_scheme      TEXT,
    classification_id          TEXT,
    classification_description TEXT,
    PRIMARY KEY (ocid, item_id)
);

CREATE TABLE item_additional_classification (
    ocid        TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    scheme      TEXT,
    code        TEXT,
    description TEXT,
    PRIMARY KEY (ocid, item_id, scheme, code)
);

-- ocds_lots_extension: a process may be split into lots.
CREATE TABLE lot (
    ocid                  TEXT NOT NULL REFERENCES process(ocid),
    lot_id                TEXT NOT NULL,
    title                 TEXT,
    status                TEXT,
    contract_period_start TEXT,
    contract_period_end   TEXT,
    PRIMARY KEY (ocid, lot_id)
);

-- Bid amounts submitted by tenderers (SEAO flat list, not OCDS bids.details).
-- party_id references party(ocid, party_id); value_unit is a raw SEAO code.
CREATE TABLE bid (
    bid_rowid    INTEGER PRIMARY KEY AUTOINCREMENT,
    ocid         TEXT NOT NULL REFERENCES process(ocid),
    party_id     TEXT,
    value_amount REAL,
    value_unit   TEXT
);

CREATE TABLE bid_related_lot (
    bid_rowid INTEGER NOT NULL REFERENCES bid(bid_rowid),
    ocid      TEXT,
    lot_id    TEXT
);

CREATE TABLE award (
    ocid               TEXT NOT NULL REFERENCES process(ocid),
    award_id           TEXT NOT NULL,
    status             TEXT,
    date               TEXT,
    value_amount       REAL,
    value_currency     TEXT,
    value_total_amount REAL,
    PRIMARY KEY (ocid, award_id)
);

CREATE TABLE award_supplier (
    ocid          TEXT NOT NULL,
    award_id      TEXT NOT NULL,
    party_id      TEXT NOT NULL,
    supplier_name TEXT,
    PRIMARY KEY (ocid, award_id, party_id)
);

-- award_id references award(ocid, award_id) (logical join, not enforced).
CREATE TABLE contract (
    ocid           TEXT NOT NULL REFERENCES process(ocid),
    contract_id    TEXT NOT NULL,
    award_id       TEXT,
    status         TEXT,
    value_amount   REAL,
    value_currency TEXT,
    date_signed    TEXT,
    period_end     TEXT,
    PRIMARY KEY (ocid, contract_id)
);

CREATE TABLE contract_amendment (
    ocid         TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    amendment_id TEXT,
    date         TEXT,
    rationale    TEXT
);

CREATE TABLE contract_transaction (
    ocid           TEXT NOT NULL,
    contract_id    TEXT NOT NULL,
    transaction_id TEXT,
    source         TEXT,
    date           TEXT,
    value_amount   REAL
);

CREATE TABLE related_process (
    ocid         TEXT NOT NULL REFERENCES process(ocid),
    related_id   TEXT,
    relationship TEXT,
    title        TEXT,
    scheme       TEXT,
    identifier   TEXT
);

-- Machine-readable data dictionary, surfaced to the LLM by the MCP server.
CREATE TABLE schema_doc (
    table_name  TEXT NOT NULL,
    column_name TEXT,
    description TEXT NOT NULL
);

-- Indexes on columns the LLM will filter / join / aggregate on.
CREATE INDEX idx_party_neq            ON party(neq);
CREATE INDEX idx_party_name           ON party(name);
CREATE INDEX idx_party_role_role      ON party_role(role);
CREATE INDEX idx_award_supplier_name  ON award_supplier(supplier_name);
CREATE INDEX idx_award_value          ON award(value_amount);
CREATE INDEX idx_award_date           ON award(date);
CREATE INDEX idx_contract_award       ON contract(award_id);
CREATE INDEX idx_contract_value       ON contract(value_amount);
CREATE INDEX idx_contract_signed      ON contract(date_signed);
CREATE INDEX idx_item_classification  ON item(classification_id);
CREATE INDEX idx_tender_status        ON tender(status);
CREATE INDEX idx_process_buyer_name   ON process(buyer_name);

-- Accent-insensitive French full-text search, one row per process.
CREATE VIRTUAL TABLE process_fts USING fts5(
    ocid UNINDEXED,
    title,
    buyer_name,
    supplier_names,
    item_descriptions,
    party_names,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Convenience views (denormalized joins for common questions).
CREATE VIEW v_contracts AS
SELECT c.ocid, t.title AS tender_title, p.buyer_name,
       c.contract_id, c.award_id,
       (SELECT group_concat(s.supplier_name, '; ')
          FROM award_supplier s
         WHERE s.ocid = c.ocid AND s.award_id = c.award_id) AS suppliers,
       c.value_amount, c.value_currency, c.status, c.date_signed, c.period_end
FROM contract c
JOIN process p ON p.ocid = c.ocid
LEFT JOIN tender t ON t.ocid = c.ocid;

CREATE VIEW v_awards AS
SELECT a.ocid, t.title AS tender_title, p.buyer_name,
       a.award_id, a.status, a.date,
       a.value_amount, a.value_currency, a.value_total_amount,
       (SELECT group_concat(s.supplier_name, '; ')
          FROM award_supplier s
         WHERE s.ocid = a.ocid AND s.award_id = a.award_id) AS suppliers
FROM award a
JOIN process p ON p.ocid = a.ocid
LEFT JOIN tender t ON t.ocid = a.ocid;

CREATE VIEW v_tenders AS
SELECT t.ocid, t.title, t.status, t.procurement_method,
       t.main_procurement_category, p.buyer_name, t.number_of_tenderers,
       t.start_date, t.end_date,
       (SELECT i.classification_id FROM item i WHERE i.ocid = t.ocid LIMIT 1)
           AS main_unspsc,
       (SELECT i.classification_description FROM item i WHERE i.ocid = t.ocid LIMIT 1)
           AS main_unspsc_desc
FROM tender t
JOIN process p ON p.ocid = t.ocid;

-- Aggregated supplier activity. Suppliers are mostly 1 per award, so summing
-- the award value per supplier is a close approximation of amount won.
CREATE VIEW v_supplier_stats AS
SELECT COALESCE(pt.neq, s.supplier_name) AS supplier_key,
       s.supplier_name,
       pt.neq,
       COUNT(*)             AS award_count,
       SUM(a.value_amount)  AS total_won
FROM award_supplier s
JOIN award a  ON a.ocid = s.ocid AND a.award_id = s.award_id
LEFT JOIN party pt ON pt.ocid = s.ocid AND pt.party_id = s.party_id
GROUP BY supplier_key;

CREATE VIEW v_buyer_stats AS
SELECT p.buyer_name,
       COUNT(DISTINCT p.ocid) AS process_count,
       SUM(a.value_amount)    AS total_awarded
FROM process p
LEFT JOIN award a ON a.ocid = p.ocid
GROUP BY p.buyer_name;
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def pick(d, *keys):
    """Return the first present, non-None value among case-variant keys.

    The source mixes ``NEQ``/``neq``, ``Municipal``/``municipal``,
    ``totalAmount``/``totalamount``, ``durationInDay``/``durationInDays``.
    """
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def to_text(x):
    """Stringify an id that may arrive as int or str; keep None as None."""
    return None if x is None else str(x)


def to_real(x):
    """Coerce a numeric-ish value to float, or None when not parseable."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(value):
    """Parse an OCDS ``value`` (dict {amount, currency, totalAmount} or scalar).

    Returns (amount, currency, total_amount).
    """
    if isinstance(value, dict):
        amount = to_real(value.get("amount"))
        currency = value.get("currency")
        total = to_real(pick(value, "totalAmount", "totalamount"))
        return amount, currency, total
    return to_real(value), None, None


def join_list(value):
    """Render a list (e.g. ``relationship``) as a comma-separated string."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
def ingest(conn, records):
    """Normalize OCDS releases into the relational tables (single transaction)."""
    cur = conn.cursor()

    rows = {name: [] for name in (
        "process", "process_tag", "party", "party_role", "tender",
        "tender_category", "document", "item", "item_additional_classification",
        "lot", "award", "award_supplier", "contract", "contract_amendment",
        "contract_transaction", "related_process",
    )}
    bids = []           # (ocid, party_id, amount, unit, [lot_id, ...])
    fts_rows = []       # text aggregated per process, for process_fts

    for rec in records:
        ocid = rec.get("ocid")
        if not ocid:
            continue
        buyer = rec.get("buyer") or {}

        rows["process"].append((
            ocid, to_text(rec.get("id")), rec.get("date"), rec.get("language"),
            rec.get("initiationType"), to_text(buyer.get("id")), buyer.get("name"),
        ))
        for tag in rec.get("tag") or []:
            rows["process_tag"].append((ocid, tag))

        # ---- parties --------------------------------------------------------
        party_names = []
        for p in rec.get("parties") or []:
            pid = to_text(p.get("id"))
            addr = p.get("address") or {}
            details = p.get("details") or {}
            rows["party"].append((
                ocid, pid, p.get("name"),
                to_text(pick(details, "NEQ", "neq")),
                to_text(pick(details, "Municipal", "municipal")),
                addr.get("streetAddress"), addr.get("locality"), addr.get("region"),
                addr.get("countryName"), addr.get("postalCode"),
            ))
            for role in p.get("roles") or []:
                rows["party_role"].append((ocid, pid, role))
            if p.get("name"):
                party_names.append(p["name"])

        # ---- tender ---------------------------------------------------------
        tender = rec.get("tender") or {}
        pe = tender.get("procuringEntity") or {}
        period = tender.get("tenderPeriod") or {}
        t_amount, _, _ = money(tender.get("value"))
        min_amount, _, _ = money(tender.get("minValue"))
        rows["tender"].append((
            ocid, to_text(tender.get("id")), tender.get("title"),
            tender.get("status"), tender.get("procurementMethod"),
            tender.get("procurementMethodDetails"),
            tender.get("procurementMethodRationale"),
            tender.get("mainProcurementCategory"), to_text(tender.get("deliveryarea")),
            tender.get("numberOfTenderers"),
            to_text(pe.get("id")), pe.get("name"),
            period.get("startDate"), period.get("endDate"),
            pick(period, "durationInDays", "durationInDay"),
            t_amount, min_amount,
        ))
        for cat in tender.get("additionalProcurementCategories") or []:
            rows["tender_category"].append((ocid, cat))
        for doc in tender.get("documents") or []:
            rows["document"].append((ocid, to_text(doc.get("id")), doc.get("url")))

        # ---- items ----------------------------------------------------------
        item_descs = []
        for it in tender.get("items") or []:
            item_id = to_text(it.get("id"))
            cls = it.get("classification") or {}
            rows["item"].append((
                ocid, item_id, it.get("description"),
                cls.get("scheme"), to_text(cls.get("id")), cls.get("description"),
            ))
            if it.get("description"):
                item_descs.append(it["description"])
            if cls.get("description"):
                item_descs.append(cls["description"])
            for ac in it.get("additionalClassifications") or []:
                rows["item_additional_classification"].append((
                    ocid, item_id, ac.get("scheme"), to_text(ac.get("id")),
                    ac.get("description"),
                ))

        # ---- lots (ocds_lots_extension) ------------------------------------
        for lot in tender.get("lots") or []:
            cp = lot.get("contractPeriod") or {}
            rows["lot"].append((
                ocid, to_text(lot.get("id")), lot.get("title"), lot.get("status"),
                cp.get("startDate"), cp.get("endDate"),
            ))

        # ---- bids -----------------------------------------------------------
        for b in rec.get("bids") or []:
            bids.append((
                ocid, to_text(b.get("id")), to_real(b.get("value")),
                to_text(b.get("valueUnit")),
                [to_text(x) for x in (b.get("relatedLots") or [])],
            ))

        # ---- awards ---------------------------------------------------------
        supplier_names = []
        for a in rec.get("awards") or []:
            award_id = to_text(a.get("id"))
            amount, currency, total = money(a.get("value"))
            rows["award"].append((
                ocid, award_id, a.get("status"), a.get("date"),
                amount, currency, total,
            ))
            for s in a.get("suppliers") or []:
                rows["award_supplier"].append((
                    ocid, award_id, to_text(s.get("id")), s.get("name"),
                ))
                if s.get("name"):
                    supplier_names.append(s["name"])

        # ---- contracts ------------------------------------------------------
        for c in rec.get("contracts") or []:
            contract_id = to_text(c.get("id"))
            amount, currency, _ = money(c.get("value"))
            cperiod = c.get("period") or {}
            rows["contract"].append((
                ocid, contract_id, to_text(c.get("awardID")), c.get("status"),
                amount, currency, c.get("dateSigned"), cperiod.get("endDate"),
            ))
            for am in c.get("amendments") or []:
                rows["contract_amendment"].append((
                    ocid, contract_id, to_text(am.get("id")),
                    am.get("date"), am.get("rationale"),
                ))
            for tr in (c.get("implementation") or {}).get("transactions") or []:
                tr_amount, _, _ = money(tr.get("value"))
                rows["contract_transaction"].append((
                    ocid, contract_id, to_text(tr.get("id")),
                    tr.get("source"), tr.get("date"), tr_amount,
                ))

        # ---- related processes ---------------------------------------------
        for rp in rec.get("relatedProcesses") or []:
            rows["related_process"].append((
                ocid, to_text(rp.get("id")), join_list(rp.get("relationship")),
                rp.get("title"), rp.get("scheme"), rp.get("identifier"),
            ))

        # ---- full-text aggregate -------------------------------------------
        fts_rows.append((
            ocid, tender.get("title"), buyer.get("name"),
            " ".join(dict.fromkeys(supplier_names)),
            " ".join(item_descs),
            " ".join(dict.fromkeys(party_names)),
        ))

    # ---- bulk insert (parents before children for FK integrity) -------------
    inserts = {
        "process": 7, "process_tag": 2, "party": 10, "party_role": 3,
        "tender": 17, "tender_category": 2, "document": 3, "item": 6,
        "item_additional_classification": 5, "lot": 6, "award": 7,
        "award_supplier": 4, "contract": 8, "contract_amendment": 5,
        "contract_transaction": 6, "related_process": 6,
    }
    order = [
        "process", "process_tag", "party", "party_role", "tender",
        "tender_category", "document", "item", "item_additional_classification",
        "lot", "award", "award_supplier", "contract", "contract_amendment",
        "contract_transaction", "related_process",
    ]
    for name in order:
        ncols = inserts[name]
        cur.executemany(
            f"INSERT OR IGNORE INTO {name} VALUES ({','.join('?' * ncols)})",
            rows[name],
        )

    # bids carry a child list (related lots), so insert row-by-row to get rowids
    for ocid, party_id, amount, unit, lot_ids in bids:
        cur.execute(
            "INSERT INTO bid (ocid, party_id, value_amount, value_unit) "
            "VALUES (?, ?, ?, ?)",
            (ocid, party_id, amount, unit),
        )
        bid_rowid = cur.lastrowid
        cur.executemany(
            "INSERT INTO bid_related_lot (bid_rowid, ocid, lot_id) VALUES (?, ?, ?)",
            [(bid_rowid, ocid, lid) for lid in lot_ids],
        )

    cur.executemany(
        "INSERT INTO process_fts "
        "(ocid, title, buyer_name, supplier_names, item_descriptions, party_names) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        fts_rows,
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Data dictionary
# --------------------------------------------------------------------------- #
SCHEMA_DOC = [
    # (table, column, description)
    ("process", None, "One row per OCDS contracting process (ocid is unique). SEAO public procurement, security services."),
    ("process", "ocid", "Open Contracting ID. Primary key joining every other table."),
    ("process", "buyer_id", "Release-scoped id of the buying organisation; join party on (ocid, buyer_id)."),
    ("party", None, "Organisations in a process. party_id is release-scoped: always join on (ocid, party_id)."),
    ("party", "neq", "Quebec enterprise number (NEQ). Stable cross-process identifier for suppliers."),
    ("party", "municipal", "SEAO flag: '1' if the buyer is a municipal body."),
    ("party_role", "role", "One of: buyer, supplier, tenderer (a party may hold several)."),
    ("tender", None, "Tender details, 1:1 with process. Amounts in CAD."),
    ("tender", "value_amount", "Estimated tender value in CAD (often absent)."),
    ("tender", "duration_days", "Tender period duration in days."),
    ("tender_category", "category", "Free-text additional procurement categories."),
    ("item", None, "Line items of the tender with their UNSPSC classification."),
    ("item", "classification_id", "Main UNSPSC code; useful to filter by service type."),
    ("item_additional_classification", "scheme", "Either UNSPSC or SEAO CATEGORY."),
    ("lot", None, "ocds_lots_extension: lots a process is split into."),
    ("bid", None, "Bid amounts submitted by tenderers (SEAO flat list)."),
    ("bid", "party_id", "The tendering party; join party on (ocid, party_id)."),
    ("bid", "value_unit", "Raw SEAO unit code for the bid value (1, 2, 7, ... ); not a currency."),
    ("bid_related_lot", None, "Links a bid to the lot(s) it targets (ocds_lots_extension)."),
    ("award", None, "Contract awards. value_amount/currency = awarded amount in CAD."),
    ("award", "value_total_amount", "Total amount over the full term when provided (>= value_amount)."),
    ("award_supplier", "party_id", "Winning supplier; join party on (ocid, party_id) to get the NEQ."),
    ("contract", None, "Signed contracts. award_id links to award(ocid, award_id)."),
    ("contract", "value_amount", "Contract value in CAD."),
    ("contract_amendment", "rationale", "Reason for the contract amendment (French free text)."),
    ("contract_transaction", "value_amount", "Spending transaction (extra/payment) in CAD."),
    ("related_process", "relationship", "Relationship to another process, e.g. 'prior'."),
    ("process_fts", None, "FTS5 full-text index (accent-insensitive French). Use: WHERE process_fts MATCH 'gardiennage'. Join back on ocid."),
    ("v_contracts", None, "VIEW: one row per contract with tender title, buyer and supplier(s). Main 'money' view."),
    ("v_awards", None, "VIEW: awards with supplier(s), tender title and buyer."),
    ("v_tenders", None, "VIEW: tender summary with buyer and main UNSPSC classification."),
    ("v_supplier_stats", None, "VIEW: per-supplier award_count and total_won (CAD), grouped by NEQ or name."),
    ("v_buyer_stats", None, "VIEW: per-buyer process_count and total_awarded (CAD)."),
]


def populate_schema_doc(conn):
    """Fill the schema_doc data dictionary surfaced to the LLM."""
    conn.executemany(
        "INSERT INTO schema_doc (table_name, column_name, description) VALUES (?, ?, ?)",
        SCHEMA_DOC,
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
TABLES_FOR_COUNTS = [
    "process", "party", "party_role", "tender", "item", "lot", "bid",
    "bid_related_lot", "award", "award_supplier", "contract",
    "contract_amendment", "contract_transaction", "related_process",
    "process_fts", "schema_doc",
]


def build(input_path, output_path):
    """Build the SQLite database from the OCDS JSON file."""
    records = json.loads(Path(input_path).read_text(encoding="utf-8"))
    print(f"Loaded {len(records)} records from {input_path}")

    conn = sqlite3.connect(output_path)
    try:
        conn.executescript(SCHEMA)
        ingest(conn, records)
        populate_schema_doc(conn)

        # Validate referential integrity now that FKs matter.
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            print(f"WARNING: {len(violations)} foreign key violations detected.")

        print(f"\nBuilt {output_path}")
        print("Row counts:")
        for table in TABLES_FOR_COUNTS:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<32} {n}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="DATA/filtered_records.json",
                        help="OCDS JSON file (default: DATA/filtered_records.json)")
    parser.add_argument("output", nargs="?", default="DATA/seao.db",
                        help="SQLite output file (default: DATA/seao.db)")
    args = parser.parse_args()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
