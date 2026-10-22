"""Database schema for the local Stage 1 prototype."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    request_text TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    external_id TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external
    ON messages(channel, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    UNIQUE(title, version)
);
CREATE TABLE IF NOT EXISTS policy_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS action_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS record_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS record_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    source_proposal_id TEXT NOT NULL UNIQUE REFERENCES record_proposals(id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS skill_versions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source_proposal_id TEXT NOT NULL UNIQUE REFERENCES skill_proposals(id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS memory_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    content TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_document_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS system_change_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    area TEXT NOT NULL,
    title TEXT NOT NULL,
    specification TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS supplier_proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    material TEXT NOT NULL,
    source_document_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    material TEXT NOT NULL,
    source_proposal_id TEXT NOT NULL UNIQUE REFERENCES supplier_proposals(id),
    status TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    verified_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    supplier_id TEXT NOT NULL,
    material TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_tonnes TEXT NOT NULL,
    unit_price_usd TEXT NOT NULL,
    total_usd TEXT NOT NULL,
    payment_terms TEXT NOT NULL,
    delivery_terms TEXT NOT NULL,
    freight_terms TEXT NOT NULL,
    tax_terms TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS stock_receipts (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    item TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    lot_code TEXT NOT NULL,
    grade TEXT NOT NULL,
    location TEXT NOT NULL,
    quantity_tonnes TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS verified_stock_lot
    ON stock_receipts(item, lot_code) WHERE status = 'approved';
CREATE TABLE IF NOT EXISTS sales_orders (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    customer TEXT NOT NULL,
    item TEXT NOT NULL,
    quantity_tonnes TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS email_outbox (
    id TEXT PRIMARY KEY,
    po_id TEXT NOT NULL UNIQUE REFERENCES purchase_orders(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_records (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    definition_id TEXT NOT NULL REFERENCES record_definitions(id),
    values_json TEXT NOT NULL,
    status TEXT NOT NULL,
    entered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    status TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    case_id TEXT REFERENCES cases(id),
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS document_index USING fts5(
    document_id UNINDEXED,
    file_name,
    extracted_text
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(id),
    kind TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
