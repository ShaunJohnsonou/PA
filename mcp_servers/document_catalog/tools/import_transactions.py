"""Import raw transactional data into the ledger."""

import logging
from typing import Any

from ..catalog_db import CatalogDB
from ..finance.importers.csv_parser import CSVBankParser
from ..finance.importers.import_orchestrator import ImportOrchestrator
from ..finance.importers.ofx_parser import OFXParser
from ..finance.ledger_db import FinanceLedger
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_import_transactions(
    document_id: str,
    bank: str | None,
    catalog: CatalogDB,
    vault: VaultManager,
    ledger: FinanceLedger,
) -> dict[str, Any]:
    """Import CSV or OFX transactions into the ledger."""
    
    logger.info("Starting import_transactions tool for document_id=%s, bank=%s", document_id, bank)
    
    # 1. Look up document
    doc = catalog.get_by_id(document_id)
    if not doc:
        logger.warning("Document %s not found in catalog", document_id)
        return {"error": "document_not_found", "message": f"Document {document_id} not found."}
    
    # 2. Get original file path
    # Extract extension from canonical_path
    ext = ""
    if "." in doc.canonical_path:
        ext = "." + doc.canonical_path.split(".")[-1]
        
    original_path = vault.get_original_path(doc.sha256_hash, ext)
    if not original_path:
        logger.error("Original file for document %s (sha256=%s) not found in vault", document_id, doc.sha256_hash)
        return {"error": "file_not_found", "message": f"Original file for {document_id} not found in vault."}

    logger.debug("Found original file at %s", original_path)

    import os
    template_dir = os.path.join(os.path.dirname(__file__), "..", "finance", "importers", "bank_templates")
    
    # 3. Instantiate orchestrator
    orchestrator = ImportOrchestrator(
        ledger=ledger,
        csv_parser=CSVBankParser(template_dir=template_dir),
        ofx_parser=OFXParser(),
    )
    
    # 4. Route based on mime type / extension
    mime = doc.mime_type or ""
    ext_lower = ext.lower()
    
    logger.info("Routing file %s with mime='%s', ext='%s'", original_path, mime, ext_lower)
    
    if mime == "text/csv" or ext_lower == ".csv":
        return orchestrator.import_csv(original_path, document_id, bank=bank)
    elif "ofx" in mime or "qfx" in mime or ext_lower in {".ofx", ".qfx"}:
        return orchestrator.import_ofx(original_path, document_id)
    elif mime == "application/pdf" or ext_lower == ".pdf":
        return {
            "error": "use_extract_document",
            "message": "PDFs should be processed via the 'extract_document' tool with document_type='bank_statement'."
        }
    else:
        return {
            "error": "unsupported_format",
            "message": f"Cannot import transactions from format {mime} ({ext}). Supported: CSV, OFX."
        }
