"""SQLite FTS5 keyword search over document chunks.

Manages the chunks table, FTS5 virtual table, and auto-sync triggers.
Provides BM25-ranked keyword search with boolean operators.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone

from . import ChunkRecord, FTSHit

logger = logging.getLogger(__name__)


class FTS5Search:
    """SQLite FTS5 keyword search over document chunks."""

    def __init__(self, db_path: str):
        """Open the database and ensure chunks + FTS5 tables exist.

        Args:
            db_path: Path to the catalog SQLite database.
        """
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create chunks table, FTS5 virtual table, and sync triggers."""
        cur = self._conn.cursor()

        # Chunks table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id        TEXT PRIMARY KEY,
                document_id     TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                page_number     INTEGER,
                section_title   TEXT,
                text            TEXT NOT NULL,
                char_count      INTEGER NOT NULL,
                token_count     INTEGER,
                bbox            TEXT,
                embedding_model TEXT,
                embedding_dim   INTEGER,
                created_at      TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(document_id, page_number)"
        )

        # FTS5 virtual table (content-sync'd with chunks)
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                section_title,
                content='chunks',
                content_rowid='rowid'
            )
        """)

        # Reason: auto-sync triggers keep FTS5 in sync with the chunks table.
        # We use CREATE TRIGGER IF NOT EXISTS isn't available in all SQLite versions,
        # so we try/except on the creation.
        triggers = [
            (
                "chunks_ai",
                """CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text, section_title)
                    VALUES (new.rowid, new.text, new.section_title);
                END"""
            ),
            (
                "chunks_ad",
                """CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_title)
                    VALUES ('delete', old.rowid, old.text, old.section_title);
                END"""
            ),
            (
                "chunks_au",
                """CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_title)
                    VALUES ('delete', old.rowid, old.text, old.section_title);
                    INSERT INTO chunks_fts(rowid, text, section_title)
                    VALUES (new.rowid, new.text, new.section_title);
                END"""
            ),
        ]

        for name, sql in triggers:
            try:
                cur.execute(sql)
            except sqlite3.OperationalError:
                pass  # Trigger already exists

        self._conn.commit()
        logger.info("FTS5 search tables ensured in %s", self._db_path)

    def index_chunks(self, chunks: list[ChunkRecord]) -> None:
        """Insert chunks into the chunks table.

        The FTS5 sync triggers will automatically populate the
        chunks_fts virtual table.
        """
        if not chunks:
            return

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                c.chunk_id, c.document_id, c.chunk_index, c.page_number,
                c.section_title, c.text, c.char_count, c.token_count,
                c.bbox, c.embedding_model, c.embedding_dim,
                c.created_at or now,
            )
            for c in chunks
        ]

        self._conn.executemany(
            """INSERT OR REPLACE INTO chunks (
                chunk_id, document_id, chunk_index, page_number,
                section_title, text, char_count, token_count,
                bbox, embedding_model, embedding_dim, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()
        logger.info("Indexed %d chunks into FTS5", len(chunks))

    def remove_document_chunks(self, document_id: str) -> int:
        """Remove all chunks for a document.

        The delete trigger auto-removes entries from chunks_fts.
        Returns number of chunks removed.
        """
        cur = self._conn.execute(
            "DELETE FROM chunks WHERE document_id = ?", (document_id,)
        )
        self._conn.commit()
        count = cur.rowcount
        logger.info("Removed %d chunks for document %s", count, document_id[:8])
        return count

    def search(
        self,
        query: str,
        top_k: int = 10,
        filter_document_ids: list[str] | None = None,
        filter_document_type: str | None = None,
    ) -> list[FTSHit]:
        """Search using FTS5 with BM25 ranking.

        Query syntax:
            - Simple words: "bank fees" → matches both words
            - Phrases: '"monthly account fee"' → exact phrase
            - Boolean: "bank AND fees NOT penalty"
            - Prefix: "transact*" → prefix matching

        Returns results ranked by BM25 (higher rank = more relevant).
        """
        # Reason: sanitise query to prevent FTS5 syntax errors
        safe_query = _sanitise_fts_query(query)
        if not safe_query:
            return []

        try:
            if filter_document_ids:
                placeholders = ",".join("?" for _ in filter_document_ids)
                sql = f"""
                    SELECT c.chunk_id, c.document_id, c.text, c.section_title,
                           c.page_number, rank
                    FROM chunks_fts
                    JOIN chunks c ON c.rowid = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                    AND c.document_id IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?
                """
                params = [safe_query] + filter_document_ids + [top_k]
            else:
                sql = """
                    SELECT c.chunk_id, c.document_id, c.text, c.section_title,
                           c.page_number, rank
                    FROM chunks_fts
                    JOIN chunks c ON c.rowid = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """
                params = [safe_query, top_k]

            rows = self._conn.execute(sql, params).fetchall()

        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 query failed for '%s': %s", query, exc)
            return []

        results = []
        for row in rows:
            results.append(FTSHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                text=row["text"],
                section_title=row["section_title"],
                page_number=row["page_number"],
                # Reason: FTS5 rank is negative (lower = more relevant).
                # We negate it so higher = better, consistent with FAISS scores.
                rank=-row["rank"],
            ))

        return results

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        """Retrieve a single chunk by ID."""
        row = self._conn.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()

        if row is None:
            return None

        return ChunkRecord(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            chunk_index=row["chunk_index"],
            page_number=row["page_number"],
            section_title=row["section_title"],
            text=row["text"],
            char_count=row["char_count"],
            token_count=row["token_count"],
            bbox=row["bbox"],
            embedding_model=row["embedding_model"],
            embedding_dim=row["embedding_dim"],
            created_at=row["created_at"],
        )

    def get_document_chunks(self, document_id: str) -> list[ChunkRecord]:
        """Retrieve all chunks for a document, ordered by chunk_index."""
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()

        return [
            ChunkRecord(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                chunk_index=r["chunk_index"],
                page_number=r["page_number"],
                section_title=r["section_title"],
                text=r["text"],
                char_count=r["char_count"],
                token_count=r["token_count"],
                bbox=r["bbox"],
                embedding_model=r["embedding_model"],
                embedding_dim=r["embedding_dim"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


def _sanitise_fts_query(query: str) -> str:
    """Strip characters that could break FTS5 syntax."""
    # Remove special FTS5 operators that users shouldn't use directly
    sanitised = re.sub(r"[{}()\[\]^~]", " ", query)
    # Collapse whitespace
    sanitised = re.sub(r"\s+", " ", sanitised).strip()
    return sanitised
