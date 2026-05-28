"""TR-1.3 — SQLite document catalog database layer.

Manages the ``hermes_catalog.sqlite`` database that serves as the central
registry of all ingested documents.  All SQL is contained within this
module — no raw SQL elsewhere.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DuplicateHashError(Exception):
    """Raised when a document with the same SHA256 already exists."""


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class DocumentRow:
    """Represents a single row in the ``documents`` table."""

    document_id: str
    sha256_hash: str
    original_filename: str
    canonical_path: str
    mime_type: str | None = None
    file_size_bytes: int | None = None
    document_type: str | None = None
    category: str | None = None
    upload_time: str = ""
    source: str | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None
    status: str = "ingested"
    extraction_status: str = "pending"
    indexing_status: str = "pending"  # pending | indexed | failed
    financial_validation_status: str | None = None
    summary: str | None = None
    tags: str | None = None  # JSON array string
    created_at: str = ""
    updated_at: str = ""


# ── Allow-lists for safe dynamic SQL ────────────────────────────────

_SORTABLE_COLUMNS = frozenset(
    {
        "upload_time",
        "original_filename",
        "document_type",
        "category",
        "status",
        "created_at",
        "file_size_bytes",
    }
)

_SORT_ORDERS = frozenset({"asc", "desc"})


# ── Database class ──────────────────────────────────────────────────


class CatalogDB:
    """SQLite-backed document catalog with WAL mode and versioned schema."""

    CURRENT_SCHEMA_VERSION = 3

    def __init__(self, db_path: str) -> None:
        """Open (or create) the catalog database.

        Enables WAL mode for concurrent read/write safety and runs
        migrations if the schema version is behind.
        """
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        logger.info("CatalogDB opened: %s (schema v%d)", db_path, self.CURRENT_SCHEMA_VERSION)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    # ── Schema management ───────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        cur = self._conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id                TEXT PRIMARY KEY,
                sha256_hash                TEXT NOT NULL UNIQUE,
                original_filename          TEXT NOT NULL,
                canonical_path             TEXT NOT NULL,
                mime_type                  TEXT,
                file_size_bytes            INTEGER,
                document_type              TEXT,
                category                   TEXT,
                upload_time                TEXT NOT NULL,
                source                     TEXT,
                date_range_start           TEXT,
                date_range_end             TEXT,
                status                     TEXT NOT NULL DEFAULT 'ingested',
                extraction_status          TEXT DEFAULT 'pending',
                indexing_status            TEXT DEFAULT 'pending',
                financial_validation_status TEXT,
                summary                    TEXT,
                tags                       TEXT,
                created_at                 TEXT NOT NULL,
                updated_at                 TEXT NOT NULL
            )
            """
        )

        # Reason: document_categories stores the registry of valid
        # life-area categories. Seeded with defaults, extensible by agent.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS document_categories (
                name        TEXT PRIMARY KEY,
                description TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(sha256_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_date "
            "ON documents(date_range_start, date_range_end)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)")

        self._seed_default_categories(cur)

        # Upsert schema version
        row = cur.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (self.CURRENT_SCHEMA_VERSION,),
            )
        else:
            current = row[0]
            if current < self.CURRENT_SCHEMA_VERSION:
                self._run_migrations(current)

        self._conn.commit()

    # Reason: default life-area categories seeded on first run.
    # Agent can add more via manage_categories tool.
    _DEFAULT_CATEGORIES = [
        ("finance", "Bank statements, invoices, tax documents, budgets"),
        ("medicine", "Medical records, prescriptions, lab results, health insurance"),
        ("fitness", "Gym memberships, training plans, health apps"),
        ("work", "Employment contracts, work correspondence, professional development"),
        ("real_estate", "Property deeds, lease agreements, mortgage documents, rates"),
        ("family", "Family documents, school reports, certificates"),
        ("education", "Degrees, transcripts, course materials, student loans"),
        ("insurance", "Insurance policies, claims, quotes"),
        ("legal", "Contracts, legal correspondence, court documents, wills"),
        ("automotive", "Vehicle registration, service records, traffic fines"),
        ("personal", "ID documents, passports, personal correspondence"),
        ("utilities", "Electricity, water, internet, phone bills"),
    ]

    def _seed_default_categories(self, cur: sqlite3.Cursor) -> None:
        """Insert default categories if the table is empty."""
        count = cur.execute("SELECT COUNT(*) FROM document_categories").fetchone()[0]
        if count > 0:
            return
        now = _utc_now()
        for name, desc in self._DEFAULT_CATEGORIES:
            cur.execute(
                "INSERT OR IGNORE INTO document_categories (name, description, created_at) "
                "VALUES (?, ?, ?)",
                (name, desc, now),
            )
        logger.info("Seeded %d default document categories", len(self._DEFAULT_CATEGORIES))

    def _run_migrations(self, from_version: int) -> None:
        """Apply sequential migrations from *from_version*."""
        cur = self._conn.cursor()

        if from_version < 2:
            try:
                cur.execute(
                    "ALTER TABLE documents ADD COLUMN indexing_status TEXT DEFAULT 'pending'"
                )
                logger.info("Migration v1→v2: added indexing_status column")
            except sqlite3.OperationalError:
                pass

        if from_version < 3:
            # Reason: v3 adds document categories for life-area organisation
            try:
                cur.execute("ALTER TABLE documents ADD COLUMN category TEXT")
                logger.info("Migration v2→v3: added category column to documents")
            except sqlite3.OperationalError:
                pass
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS document_categories (
                    name        TEXT PRIMARY KEY,
                    description TEXT,
                    created_at  TEXT NOT NULL
                )
                """
            )
            self._seed_default_categories(cur)
            logger.info("Migration v2→v3: created document_categories table")

        cur.execute(
            "UPDATE schema_version SET version = ?",
            (self.CURRENT_SCHEMA_VERSION,),
        )
        logger.info("Migrated schema from v%d to v%d", from_version, self.CURRENT_SCHEMA_VERSION)

    # ── Document CRUD ───────────────────────────────────────────────

    def insert_document(self, doc: DocumentRow) -> None:
        """Insert a new document row.

        Raises:
            DuplicateHashError: If a document with the same ``sha256_hash``
                already exists.
        """
        now = _utc_now()
        if not doc.created_at:
            doc.created_at = now
        if not doc.updated_at:
            doc.updated_at = now
        if not doc.upload_time:
            doc.upload_time = now

        try:
            self._conn.execute(
                """
                INSERT INTO documents (
                    document_id, sha256_hash, original_filename, canonical_path,
                    mime_type, file_size_bytes, document_type, category, upload_time,
                    source, date_range_start, date_range_end, status,
                    extraction_status, financial_validation_status,
                    summary, tags, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.document_id,
                    doc.sha256_hash,
                    doc.original_filename,
                    doc.canonical_path,
                    doc.mime_type,
                    doc.file_size_bytes,
                    doc.document_type,
                    doc.category,
                    doc.upload_time,
                    doc.source,
                    doc.date_range_start,
                    doc.date_range_end,
                    doc.status,
                    doc.extraction_status,
                    doc.financial_validation_status,
                    doc.summary,
                    doc.tags,
                    doc.created_at,
                    doc.updated_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            if "sha256_hash" in str(exc):
                raise DuplicateHashError(
                    f"Document with hash {doc.sha256_hash} already exists"
                ) from exc
            raise

    def get_by_hash(self, sha256_hash: str) -> DocumentRow | None:
        """Lookup a document by its SHA256 hash."""
        row = self._conn.execute(
            "SELECT * FROM documents WHERE sha256_hash = ?", (sha256_hash,)
        ).fetchone()
        return _row_to_doc(row) if row else None

    def get_by_id(self, document_id: str) -> DocumentRow | None:
        """Lookup a document by its UUID."""
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return _row_to_doc(row) if row else None

    def update_document(self, document_id: str, **fields: Any) -> None:
        """Update specific fields on a document row.

        Automatically sets ``updated_at`` to the current UTC time.
        """
        if not fields:
            return
        fields["updated_at"] = _utc_now()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [document_id]

        self._conn.execute(
            f"UPDATE documents SET {set_clause} WHERE document_id = ?",  # noqa: S608
            values,
        )
        self._conn.commit()

    def list_documents(
        self,
        document_type: str | None = None,
        category: str | None = None,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "upload_time",
        sort_order: str = "desc",
    ) -> tuple[list[DocumentRow], int]:
        """Query documents with composable filters.

        Args:
            document_type: Filter by ``document_type``.
            status: Filter by ``status``.
            date_from: ISO date — ``date_range_start >= date_from``.
            date_to: ISO date — ``date_range_end <= date_to``.
            search: Substring match on ``original_filename``.
            limit: Max results (clamped to 1–100).
            offset: Pagination offset (clamped to >= 0).
            sort_by: Column to sort by (validated against allow-list).
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            Tuple of ``(rows, total_count)`` for pagination.

        Raises:
            ValueError: If *sort_by* is not in the allow-list.
        """
        # Validate sort_by against allow-list to prevent SQL injection
        if sort_by not in _SORTABLE_COLUMNS:
            raise ValueError(
                f"Invalid sort_by '{sort_by}'. "
                f"Allowed: {sorted(_SORTABLE_COLUMNS)}"
            )
        if sort_order not in _SORT_ORDERS:
            sort_order = "desc"

        # Clamp pagination
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        where_parts: list[str] = []
        params: list[Any] = []

        if document_type:
            where_parts.append("document_type = ?")
            params.append(document_type)
        if category:
            where_parts.append("category = ?")
            params.append(category)
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if date_from:
            where_parts.append("date_range_start >= ?")
            params.append(date_from)
        if date_to:
            where_parts.append("date_range_end <= ?")
            params.append(date_to)
        if search:
            where_parts.append("original_filename LIKE ?")
            params.append(f"%{search[:200]}%")

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Get total count
        count_sql = f"SELECT COUNT(*) FROM documents{where_clause}"  # noqa: S608
        total = self._conn.execute(count_sql, params).fetchone()[0]

        # Get paginated results
        query_sql = (
            f"SELECT * FROM documents{where_clause} "  # noqa: S608
            f"ORDER BY {sort_by} {sort_order} "
            f"LIMIT ? OFFSET ?"
        )
        rows = self._conn.execute(query_sql, params + [limit, offset]).fetchall()

        return [_row_to_doc(r) for r in rows], total

    def delete_document(self, document_id: str) -> bool:
        """Remove a document from the catalog.

        Returns:
            ``True`` if a row was deleted.
        """
        cur = self._conn.execute(
            "DELETE FROM documents WHERE document_id = ?", (document_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Category CRUD ─────────────────────────────────────────────

    def list_categories(self) -> list[dict]:
        """Return all registered document categories."""
        rows = self._conn.execute(
            "SELECT name, description, created_at FROM document_categories ORDER BY name"
        ).fetchall()
        return [{"name": r[0], "description": r[1], "created_at": r[2]} for r in rows]

    def add_category(self, name: str, description: str = "") -> bool:
        """Register a new document category.

        Returns:
            ``True`` if the category was created, ``False`` if it
            already existed.
        """
        try:
            self._conn.execute(
                "INSERT INTO document_categories (name, description, created_at) "
                "VALUES (?, ?, ?)",
                (name.lower().strip(), description, _utc_now()),
            )
            self._conn.commit()
            logger.info("Created new category: %s", name)
            return True
        except sqlite3.IntegrityError:
            logger.debug("Category '%s' already exists", name)
            return False

    def get_documents_by_category(self, category: str) -> list[DocumentRow]:
        """Return all documents in a given category."""
        rows = self._conn.execute(
            "SELECT * FROM documents WHERE category = ? ORDER BY upload_time DESC",
            (category,),
        ).fetchall()
        return [_row_to_doc(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_doc(row: sqlite3.Row) -> DocumentRow:
    """Convert a ``sqlite3.Row`` to a ``DocumentRow``."""
    return DocumentRow(**{k: row[k] for k in row.keys()})
