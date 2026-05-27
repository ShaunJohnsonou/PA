"""document_catalog_mcp — unified MCP server entry point.

Exposes document ingestion, listing, extraction, page retrieval,
indexing, and search tools over stdio transport. Initialises the
vault, SQLite catalog, extraction engines, and search subsystem
on startup.
"""
from __future__ import annotations

import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .catalog_db import CatalogDB
from .vault import VaultManager
from .tools.ingest import handle_ingest_document
from .tools.list_docs import handle_list_documents
from .tools.extract import handle_extract_document
from .tools.get_page import handle_get_document_page
from .tools.index import handle_index_document
from .tools.search import handle_search_documents
from .tools.delete import handle_delete_document
from .tools.query_transactions import handle_query_transactions
from .tools.financial_coverage import handle_get_financial_coverage
from .tools.transaction_evidence import handle_get_transaction_evidence
from .tools.spending_analysis import handle_run_spending_analysis
from .tools.find_anomalies import handle_find_anomalies
from .tools.review import handle_get_validation_issues, handle_override_validation, handle_set_document_status
from .tools.report import handle_run_financial_report
from .tools.import_transactions import handle_import_transactions
from .tools.split_pdf import handle_split_pdf
from .tools.finalize_extraction import handle_finalize_extraction

logger = logging.getLogger("document_catalog_mcp")

# ── Server instance ─────────────────────────────────────────────

app = Server("document_catalog_mcp")

# Global state — initialised in main()
_vault: VaultManager | None = None
_catalog: CatalogDB | None = None
_docling_engine = None
_markitdown_engine = None
_pdfplumber_engine = None
_lifecycle = None
_faiss_index = None
_fts_search = None
_embedding_service = None
_finance_ledger = None
_finance_pipeline = None
_finance_validator = None


# ── Tool definitions ────────────────────────────────────────────

TOOLS = [
    Tool(
        name="ingest_document",
        description=(
            "Ingest a document into the vault. Computes SHA256 hash, "
            "detects MIME type, stores the original immutably, and "
            "creates a catalog entry. Returns document_id and dedup status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Local path to the file to ingest",
                },
                "source": {
                    "type": "string",
                    "description": 'Origin: "telegram_upload", "manual", "email"',
                    "default": "manual",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional user-provided tags",
                    "default": [],
                },
                "document_type": {
                    "type": "string",
                    "description": (
                        "Optional type override: structured_record, generic_document, etc."
                    ),
                },
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="list_documents",
        description=(
            "Query the document catalog with composable filters. "
            "Returns paginated, sorted results with metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_type": {
                    "type": "string",
                    "description": "Filter by document type",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status",
                },
                "date_from": {
                    "type": "string",
                    "description": "ISO date — filter date_range_start >=",
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO date — filter date_range_end <=",
                },
                "search": {
                    "type": "string",
                    "description": "Substring match on original_filename",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per page (1-100)",
                    "default": 20,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Column to sort by",
                    "default": "upload_time",
                },
                "sort_order": {
                    "type": "string",
                    "description": "asc or desc",
                    "default": "desc",
                },
            },
        },
    ),
    Tool(
        name="extract_document",
        description=(
            "Extract text, tables, and structure from an ingested document. "
            "Uses Docling for PDFs and MarkItDown for other formats. "
            "When 'page' is specified, extracts only that single page from a "
            "previously split PDF (call split_pdf first). "
            "Returns extraction summary with page count, table count, and engine used."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document to extract (from ingest or list)",
                },
                "force": {
                    "type": "boolean",
                    "description": "If true, re-extract even if already done",
                    "default": False,
                },
                "page": {
                    "type": "integer",
                    "description": (
                        "Page number to extract (1-indexed). "
                        "Only for PDFs that have been split via split_pdf. "
                        "Omit to extract the entire document."
                    ),
                },
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="split_pdf",
        description=(
            "Split a multi-page PDF into individual single-page PDF files. "
            "Must be called before per-page extraction. Returns the page count "
            "and list of pages. After splitting, extract each page individually "
            "using extract_document with page=N, then call finalize_extraction."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the ingested PDF document to split",
                },
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="finalize_extraction",
        description=(
            "Combine per-page extraction results into a unified document. "
            "Call this after extracting all pages individually. "
            "Combines page markdowns, runs document classification, "
            "and triggers the financial pipeline for bank statements/invoices "
            "to import transactions into the ledger."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document whose pages have been extracted",
                },
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="get_document_page",
        description=(
            "Retrieve the extracted text content of a specific page from a document. "
            "Returns the page text, word count, and any tables found on that page. "
            "The document must have been extracted first using extract_document."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document",
                },
                "page_number": {
                    "type": "integer",
                    "description": "Page number to retrieve (1-indexed)",
                },
                "include_tables": {
                    "type": "boolean",
                    "description": "Include structured table data for this page",
                    "default": True,
                },
            },
            "required": ["document_id", "page_number"],
        },
    ),
    Tool(
        name="index_document",
        description=(
            "Index an extracted document for search. Splits the document into "
            "chunks, generates embeddings, and adds them to the FAISS and FTS5 "
            "indexes. The document must be extracted first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document to index",
                },
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="search_documents",
        description=(
            "Search across all indexed documents using keyword, semantic, or "
            "hybrid search. Returns ranked passages with source citations. "
            "Use 'keyword' mode for reference numbers and exact terms, "
            "'semantic' for natural language questions, or 'hybrid' (default) "
            "for best coverage."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query text",
                },
                "mode": {
                    "type": "string",
                    "description": "Search mode: 'keyword', 'semantic', or 'hybrid'",
                    "default": "hybrid",
                },
                "document_type": {
                    "type": "string",
                    "description": "Filter results to a specific document type",
                },
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter results to specific document UUIDs",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (1-50)",
                    "default": 10,
                },
                "include_text": {
                    "type": "boolean",
                    "description": "Include chunk text in results",
                    "default": True,
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum relevance threshold (0.0 to 1.0)",
                    "default": 0.0,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="delete_document",
        description=(
            "Permanently delete a document from the vault, search indexes, "
            "and catalog. This removes the original file, extraction artifacts, "
            "FAISS vectors, and FTS5 chunks. This action cannot be undone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document to delete",
                },
            },
            "required": ["document_id"],
        },
    ),
    Tool(
        name="query_transactions",
        description=(
            "Query financial transactions from the ledger. Supports filtering "
            "by account, date range, category, merchant, and amount. Supports "
            "aggregation with group_by (month/category/merchant) and metrics "
            "(sum/count/avg/min/max). By default only includes validated data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Filter by account UUID"},
                "date_from": {"type": "string", "description": "ISO date — start of range"},
                "date_to": {"type": "string", "description": "ISO date — end of range"},
                "category": {"type": "string", "description": "Filter by category"},
                "merchant": {"type": "string", "description": "Filter by merchant"},
                "description_contains": {"type": "string", "description": "Substring search in description"},
                "amount_min_cents": {"type": "integer", "description": "Minimum amount (cents)"},
                "amount_max_cents": {"type": "integer", "description": "Maximum amount (cents)"},
                "group_by": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Group results: month, category, merchant, account",
                },
                "metrics": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Aggregation metrics: sum, count, avg, min, max",
                },
                "order_by": {"type": "string", "default": "transaction_date"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "only_validated": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="get_financial_coverage",
        description=(
            "Check what financial data is available. Returns account summaries, "
            "date ranges, gap analysis (months with no statements), and "
            "validation status. Call this before financial queries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Optional account filter"},
                "start_date": {"type": "string", "description": "ISO date — coverage check start"},
                "end_date": {"type": "string", "description": "ISO date — coverage check end"},
            },
        },
    ),
    Tool(
        name="get_transaction_evidence",
        description=(
            "Retrieve extraction provenance for a transaction: source document, "
            "page, row, raw PDF text, and validation status. Use to cite sources."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "transaction_id": {
                    "type": "string",
                    "description": "UUID of the transaction",
                },
            },
            "required": ["transaction_id"],
        },
    ),
    Tool(
        name="run_spending_analysis",
        description=(
            "Analyse spending over a date range. Returns income/expense totals, "
            "category or merchant breakdowns, recurring payment detection, and "
            "coverage warnings. All amounts in integer cents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date — period start"},
                "end_date": {"type": "string", "description": "ISO date — period end"},
                "account_id": {"type": "string", "description": "Optional account filter"},
                "group_by": {
                    "type": "string",
                    "description": "Group by: category, merchant, or month",
                    "default": "category",
                },
                "top_n": {"type": "integer", "default": 10},
                "only_validated": {"type": "boolean", "default": True},
            },
            "required": ["start_date", "end_date"],
        },
    ),
    Tool(
        name="find_anomalies",
        description=(
            "Scan transactions for anomalies: unusually large transactions, "
            "new merchants, duplicate charges, and category spending spikes. "
            "Sensitivity: low, medium, high."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date — scan start"},
                "end_date": {"type": "string", "description": "ISO date — scan end"},
                "account_id": {"type": "string", "description": "Optional account filter"},
                "sensitivity": {
                    "type": "string",
                    "description": "Detection sensitivity: low, medium, high",
                    "default": "medium",
                },
            },
            "required": ["start_date", "end_date"],
        },
    ),
    Tool(
        name="get_validation_issues",
        description="Query validation results that failed. Use this to review why a financial statement was flagged as 'needs_review'.",
        inputSchema={
            "type": "object",
            "properties": {
                "statement_id": {"type": "string"},
                "document_id": {"type": "string"},
                "severity": {"type": "string", "description": "'error' or 'warning'"},
            },
        },
    ),
    Tool(
        name="override_validation",
        description="Override a failed validation result with a reason, unblocking the statement if it was an error.",
        inputSchema={
            "type": "object",
            "properties": {
                "validation_id": {"type": "string"},
                "reason": {"type": "string"},
                "overridden_by": {"type": "string"},
            },
            "required": ["validation_id", "reason"],
        },
    ),
    Tool(
        name="set_document_status",
        description="Update a document's status in the catalog (e.g., to 'excluded' or 'archived').",
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "status": {"type": "string", "description": "'active', 'excluded', or 'archived'"},
                "reason": {"type": "string"},
            },
            "required": ["document_id", "status"],
        },
    ),
    Tool(
        name="run_financial_report",
        description="Generate comprehensive financial reports (monthly_summary, annual_overview, category_breakdown). Returns formatted markdown and metrics.",
        inputSchema={
            "type": "object",
            "properties": {
                "report_type": {"type": "string", "description": "'monthly_summary', 'annual_overview', 'category_breakdown'"},
                "account_id": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": ["report_type", "start_date", "end_date"],
        },
    ),
    Tool(
        name="import_transactions",
        description="Directly import and ingest raw transactional data (CSV/OFX) into the financial ledger.",
        inputSchema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "UUID of the document to import"},
                "bank": {"type": "string", "description": "Optional bank name (for CSV parsing)"},
            },
            "required": ["document_id"],
        },
    ),
]


# ── Tool handlers ───────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return the tools this server exposes."""
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to the appropriate handler."""
    import time as _time

    assert _vault is not None, "Server not initialised"
    assert _catalog is not None, "Server not initialised"

    logger.info("Tool call: %s(%s)", name, _summarise_args(arguments))
    t0 = _time.monotonic()

    try:
        if name == "ingest_document":
            result = await handle_ingest_document(
                file_path=arguments.get("file_path", ""),
                source=arguments.get("source", "manual"),
                tags=arguments.get("tags"),
                document_type=arguments.get("document_type"),
                vault=_vault,
                catalog=_catalog,
            )
        elif name == "list_documents":
            result = await handle_list_documents(
                document_type=arguments.get("document_type"),
                status=arguments.get("status"),
                date_from=arguments.get("date_from"),
                date_to=arguments.get("date_to"),
                search=arguments.get("search"),
                limit=arguments.get("limit", 20),
                offset=arguments.get("offset", 0),
                sort_by=arguments.get("sort_by", "upload_time"),
                sort_order=arguments.get("sort_order", "desc"),
                catalog=_catalog,
            )
        elif name == "extract_document":
            result = await handle_extract_document(
                document_id=arguments.get("document_id", ""),
                force=arguments.get("force", False),
                page=arguments.get("page"),
                vault=_vault,
                catalog=_catalog,
                docling_engine=_docling_engine,
                markitdown_engine=_markitdown_engine,
                pdfplumber_engine=_pdfplumber_engine,
                finance_pipeline=_finance_pipeline,
            )
        elif name == "split_pdf":
            result = await handle_split_pdf(
                document_id=arguments.get("document_id", ""),
                vault=_vault,
                catalog=_catalog,
                pdfplumber_engine=_pdfplumber_engine,
            )
        elif name == "finalize_extraction":
            result = await handle_finalize_extraction(
                document_id=arguments.get("document_id", ""),
                vault=_vault,
                catalog=_catalog,
                pdfplumber_engine=_pdfplumber_engine,
                finance_pipeline=_finance_pipeline,
            )
        elif name == "get_document_page":
            result = await handle_get_document_page(
                document_id=arguments.get("document_id", ""),
                page_number=arguments.get("page_number", 1),
                include_tables=arguments.get("include_tables", True),
                vault=_vault,
                catalog=_catalog,
            )
        elif name == "index_document":
            result = await handle_index_document(
                document_id=arguments.get("document_id", ""),
                vault=_vault,
                catalog=_catalog,
                lifecycle=_lifecycle,
            )
        elif name == "search_documents":
            result = await handle_search_documents(
                query=arguments.get("query", ""),
                mode=arguments.get("mode", "hybrid"),
                document_type=arguments.get("document_type"),
                document_ids=arguments.get("document_ids"),
                top_k=arguments.get("top_k", 10),
                include_text=arguments.get("include_text", True),
                min_score=arguments.get("min_score", 0.0),
                catalog=_catalog,
                faiss_index=_faiss_index,
                fts_search=_fts_search,
                embedding_service=_embedding_service,
            )
        elif name == "delete_document":
            result = await handle_delete_document(
                document_id=arguments.get("document_id", ""),
                vault=_vault,
                catalog=_catalog,
                lifecycle=_lifecycle,
            )
        elif name == "query_transactions":
            result = await handle_query_transactions(
                account_id=arguments.get("account_id"),
                date_from=arguments.get("date_from"),
                date_to=arguments.get("date_to"),
                category=arguments.get("category"),
                merchant=arguments.get("merchant"),
                description_contains=arguments.get("description_contains"),
                amount_min_cents=arguments.get("amount_min_cents"),
                amount_max_cents=arguments.get("amount_max_cents"),
                group_by=arguments.get("group_by"),
                metrics=arguments.get("metrics"),
                order_by=arguments.get("order_by", "transaction_date"),
                limit=arguments.get("limit", 50),
                offset=arguments.get("offset", 0),
                only_validated=arguments.get("only_validated", True),
                ledger=_finance_ledger,
            )
        elif name == "get_financial_coverage":
            result = await handle_get_financial_coverage(
                account_id=arguments.get("account_id"),
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
                ledger=_finance_ledger,
            )
        elif name == "get_transaction_evidence":
            result = await handle_get_transaction_evidence(
                transaction_id=arguments.get("transaction_id", ""),
                ledger=_finance_ledger,
            )
        elif name == "run_spending_analysis":
            result = await handle_run_spending_analysis(
                start_date=arguments.get("start_date", ""),
                end_date=arguments.get("end_date", ""),
                account_id=arguments.get("account_id"),
                group_by=arguments.get("group_by", "category"),
                top_n=arguments.get("top_n", 10),
                only_validated=arguments.get("only_validated", True),
                ledger=_finance_ledger,
            )
        elif name == "find_anomalies":
            result = await handle_find_anomalies(
                start_date=arguments.get("start_date", ""),
                end_date=arguments.get("end_date", ""),
                account_id=arguments.get("account_id"),
                sensitivity=arguments.get("sensitivity", "medium"),
                ledger=_finance_ledger,
            )
        elif name == "get_validation_issues":
            result = await handle_get_validation_issues(
                statement_id=arguments.get("statement_id"),
                document_id=arguments.get("document_id"),
                severity=arguments.get("severity"),
                ledger=_finance_ledger,
            )
        elif name == "override_validation":
            result = await handle_override_validation(
                validation_id=arguments.get("validation_id", ""),
                reason=arguments.get("reason", ""),
                overridden_by=arguments.get("overridden_by"),
                ledger=_finance_ledger,
                audit=logging.getLogger("audit"),
            )
        elif name == "set_document_status":
            result = await handle_set_document_status(
                document_id=arguments.get("document_id", ""),
                status=arguments.get("status", ""),
                reason=arguments.get("reason"),
                catalog=_catalog,
                audit=logging.getLogger("audit"),
            )
        elif name == "run_financial_report":
            result = await handle_run_financial_report(
                report_type=arguments.get("report_type", ""),
                account_id=arguments.get("account_id"),
                start_date=arguments.get("start_date", ""),
                end_date=arguments.get("end_date", ""),
                ledger=_finance_ledger,
            )
        elif name == "import_transactions":
            result = await handle_import_transactions(
                document_id=arguments.get("document_id", ""),
                bank=arguments.get("bank"),
                catalog=_catalog,
                vault=_vault,
                ledger=_finance_ledger,
            )
        else:
            result = {"error": "unknown_tool", "message": f"Unknown tool: {name}"}

        elapsed = _time.monotonic() - t0
        is_error = "error" in result
        result_summary = result.get("error", "ok")
        logger.info(
            "Tool result: %s -> %s (%.3fs, keys=%s)",
            name, result_summary, elapsed, list(result.keys()),
        )
        if is_error:
            logger.warning("Tool %s returned error: %s", name, result.get("message", ""))

    except Exception as exc:
        elapsed = _time.monotonic() - t0
        logger.exception("Tool %s CRASHED after %.3fs: %s", name, elapsed, exc)
        result = {"error": "internal_error", "message": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _summarise_args(args: dict) -> str:
    """Create a short log-safe summary of tool arguments."""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "..."
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


# ── Entrypoint ──────────────────────────────────────────────────

async def _run() -> None:
    """Async main: initialise state and run the MCP server."""
    global _vault, _catalog, _docling_engine, _markitdown_engine, _pdfplumber_engine
    global _lifecycle, _faiss_index, _fts_search, _embedding_service
    global _finance_ledger, _finance_pipeline, _finance_validator

    vault_path = os.environ.get("HERMES_VAULT_PATH", "/hermes-vault")

    _vault = VaultManager(vault_path)
    _vault.ensure_dirs()

    _catalog = CatalogDB(_vault.catalog_path)

    # ── Phase 2: extraction engines (lazy-init) ──────────────────
    from .engines.docling_engine import DoclingEngine
    from .engines.markitdown_engine import MarkItDownEngine
    from .engines.pdfplumber_engine import PdfPlumberEngine

    _docling_engine = DoclingEngine(vault_path)
    _markitdown_engine = MarkItDownEngine(vault_path)
    _pdfplumber_engine = PdfPlumberEngine(vault_path)

    # Reason: pre-warm Docling's ML models at startup so the first
    # extract_document call doesn't block for ~3.5 minutes.
    # The server will take longer to start, but every extraction
    # call will be fast afterwards.
    _docling_engine.warm_up()

    # ── Phase 3: search subsystem ────────────────────────────────
    from .search.embeddings import EmbeddingService
    from .search.faiss_index import FaissIndexManager
    from .search.fts_search import FTS5Search
    from .search.lifecycle import IndexLifecycle

    _embedding_service = EmbeddingService()

    index_dir = os.path.join(vault_path, "indexes")
    _faiss_index = FaissIndexManager(
        index_dir, dimension=_embedding_service.dimension
    )
    _faiss_index.load_or_create()

    _fts_search = FTS5Search(_vault.catalog_path)

    _lifecycle = IndexLifecycle(
        faiss_mgr=_faiss_index,
        fts=_fts_search,
        embeddings=_embedding_service,
        vault_root=vault_path,
    )

    # ── Phase 4: financial ledger ────────────────────────────────
    from .finance.ledger_db import FinanceLedger
    from .finance.extraction_pipeline import FinancialExtractionPipeline, build_parser_registry
    from .finance.validation import ValidationEngine

    finance_db_path = os.path.join(vault_path, "finance.duckdb")
    _finance_ledger = FinanceLedger(finance_db_path)
    _finance_ledger.connect()

    _finance_pipeline = FinancialExtractionPipeline(
        ledger=_finance_ledger,
        registry=build_parser_registry(),
    )
    _finance_validator = ValidationEngine(_finance_ledger)

    tool_names = [t.name for t in TOOLS]
    logger.info("document_catalog_mcp starting, vault=%s", vault_path)
    logger.info("Registered tools: %s", tool_names)

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    """Synchronous entry point."""
    import asyncio
    import os

    try:
        import dotenv
        # Load environment variables manually because the hermes gateway
        # drops them when running the MCP child process.
        env_file = "/hermes-vault/.env"
        if os.path.isfile(env_file):
            dotenv.load_dotenv(env_file)
    except Exception as exc:
        # Ignore dotenv failure, we just won't have env vars
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Setup dedicated file logging for our custom tools
    log_dir = os.path.join("/opt/hermes", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "log.log")
    
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    
    # Target exactly our custom code namespace
    custom_logger = logging.getLogger("mcp_servers")
    custom_logger.setLevel(logging.DEBUG)
    custom_logger.addHandler(file_handler)
    
    try:
        custom_logger.info("Custom file logging initialised at %s", log_file)
        
        azure_keys = [k for k in os.environ.keys() if k.startswith("AZURE")]
        langfuse_keys = [k for k in os.environ.keys() if k.startswith("LANGFUSE")]
        custom_logger.debug("Environment variables present on startup: Azure=%s, Langfuse=%s", azure_keys, langfuse_keys)

        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
    except Exception as exc:
        custom_logger.critical("MCP Server crashed during startup: %s", exc, exc_info=True)
        import traceback
        traceback.print_exc()
        raise
    finally:
        if _catalog:
            _catalog.close()
        if _faiss_index:
            _faiss_index.close()
        if _fts_search:
            _fts_search.close()
        if _finance_ledger:
            _finance_ledger.close()

if __name__ == "__main__":
    main()
