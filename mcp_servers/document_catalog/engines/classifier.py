"""Document type and bank classifier.

Categorises documents into predefined types and identifies the
bank by reading actual document content — not just filenames.

Reason: Filename-only classification is unreliable. A file called
"CertifiedStatements.pdf" doesn't tell you which bank or document
type it is. Reading the actual content does.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Valid document types
DOCUMENT_TYPES = {
    "bank_statement",
    "invoice",
    "payslip",
    "tax_document",
    "financial_report",
    "receipt",
    "payment_slip",
    "id_document",
    "general",
}

# Reason: South African banks each have distinctive keywords that
# appear in their statements, CSVs, and transaction descriptions.
# Ordered by specificity — more unique patterns first.
BANK_SIGNATURES = {
    "ABSA": [
        "absa bank", "absahl", "absa life", "absa vehicle",
        "absa credit", "absa home", "absa savings",
    ],
    "FNB": [
        "first national bank", "fnb", "fnb online",
        "wesbank", "rand merchant",
    ],
    "Capitec": [
        "capitec", "capitec bank",
    ],
    "Nedbank": [
        "nedbank", "nedbankgreensavings", "nedbank greenbacks",
    ],
    "Standard Bank": [
        "standard bank", "sbsa", "standardbank",
    ],
    "Discovery Bank": [
        "discovery bank", "discovery", "discovery credit",
    ],
    "TymeBank": [
        "tymebank", "tyme bank",
    ],
    "African Bank": [
        "african bank",
    ],
}

# Reason: content patterns that identify document types regardless
# of filename. Ordered from most specific to least specific.
DOCUMENT_TYPE_PATTERNS = {
    "bank_statement": [
        r"statement\s+of\s+account",
        r"bank\s+statement",
        r"account\s+statement",
        r"opening\s+balance",
        r"closing\s+balance",
        r"transaction\s+history",
        r"statement\s+period",
        r"date.*description.*amount.*balance",
    ],
    "invoice": [
        r"tax\s+invoice",
        r"invoice\s+no",
        r"invoice\s+number",
        r"invoice\s+date",
        r"vat\s+no",
        r"amount\s+due",
        r"bill\s+to",
        r"payment\s+terms",
    ],
    "payslip": [
        r"payslip",
        r"salary\s+advice",
        r"earnings.*deductions",
        r"gross\s+pay",
        r"net\s+pay",
        r"uif\s+contribution",
        r"paye",
    ],
    "tax_document": [
        r"tax\s+certificate",
        r"irp5",
        r"it3",
        r"tax\s+year",
        r"sars",
        r"south\s+african\s+revenue",
    ],
    "receipt": [
        r"receipt",
        r"payment\s+received",
        r"thank\s+you\s+for\s+your\s+payment",
        r"proof\s+of\s+payment",
        r"pop\b",
    ],
    "payment_slip": [
        r"deposit\s+slip",
        r"payment\s+slip",
        r"pay-in\s+slip",
    ],
    "id_document": [
        r"identity\s+document",
        r"identity\s+number",
        r"id\s+number",
        r"republic\s+of\s+south\s+africa",
        r"department\s+of\s+home\s+affairs",
        r"passport",
    ],
}


def classify_document(
    original_filename: str,
    first_page_text: str = "",
) -> tuple[str, float]:
    """Classify a document based on its filename and content.

    Reads the content first (not just the filename) to determine
    the document type. Falls back to LLM classification if rule-based
    matching is uncertain.

    Args:
        original_filename: The original filename.
        first_page_text: Text content from the document (first page
                         or first ~2000 chars of CSV).

    Returns:
        Tuple of (document_type, confidence).
    """
    content_lower = first_page_text[:3000].lower() if first_page_text else ""
    filename_lower = original_filename.lower()

    # ── 1. Content-based classification (highest priority) ────────
    if content_lower:
        best_type = None
        best_score = 0

        for doc_type, patterns in DOCUMENT_TYPE_PATTERNS.items():
            score = sum(1 for p in patterns if re.search(p, content_lower))
            if score > best_score:
                best_score = score
                best_type = doc_type

        if best_type and best_score >= 2:
            # Reason: 2+ pattern matches = high confidence
            confidence = min(0.6 + (best_score * 0.1), 0.95)
            logger.info(
                "Content classified '%s' as %s (score=%d, confidence=%.2f)",
                original_filename, best_type, best_score, confidence,
            )
            return best_type, confidence

    # ── 2. Filename heuristics (medium priority) ─────────────────
    if "statement" in filename_lower or "estatement" in filename_lower:
        return "bank_statement", 0.7
    if "invoice" in filename_lower or "receipt" in filename_lower:
        return "invoice", 0.7
    if "payslip" in filename_lower or "salary" in filename_lower:
        return "payslip", 0.7
    if "irp5" in filename_lower or "it3" in filename_lower or "tax" in filename_lower:
        return "tax_document", 0.7
    if "slip" in filename_lower:
        return "payment_slip", 0.6
    if "id" in filename_lower or "passport" in filename_lower:
        return "id_document", 0.5

    # ── 3. Single content match (lower confidence) ───────────────
    if content_lower:
        for doc_type, patterns in DOCUMENT_TYPE_PATTERNS.items():
            if any(re.search(p, content_lower) for p in patterns):
                logger.info(
                    "Weak content match: '%s' as %s",
                    original_filename, doc_type,
                )
                return doc_type, 0.5

    # ── 4. LLM classification fallback ───────────────────────────
    llm_result = _llm_classify(original_filename, first_page_text)
    if llm_result:
        return llm_result

    # ── 5. Default ───────────────────────────────────────────────
    return "general", 0.3


def detect_bank(content: str) -> tuple[str | None, float]:
    """Detect which bank a document belongs to by reading its content.

    Args:
        content: Text content from the document (extracted text,
                 CSV rows, or any readable content).

    Returns:
        Tuple of (bank_name, confidence) or (None, 0.0).
    """
    if not content:
        return None, 0.0

    content_lower = content.lower()
    best_bank = None
    best_score = 0

    for bank_name, signatures in BANK_SIGNATURES.items():
        score = sum(1 for sig in signatures if sig in content_lower)
        if score > best_score:
            best_score = score
            best_bank = bank_name

    if best_bank and best_score >= 1:
        confidence = min(0.5 + (best_score * 0.15), 0.95)
        logger.info(
            "Detected bank '%s' from content (score=%d, confidence=%.2f)",
            best_bank, best_score, confidence,
        )
        return best_bank, confidence

    return None, 0.0


def classify_and_detect(
    original_filename: str,
    content: str = "",
) -> dict:
    """Full classification: document type + bank detection.

    This is the main entry point for document intelligence.
    Call it with the first ~2000 chars of content to get a
    complete classification.

    Returns:
        Dict with keys: document_type, type_confidence,
        bank_name, bank_confidence.
    """
    doc_type, type_conf = classify_document(original_filename, content)
    bank_name, bank_conf = detect_bank(content)

    logger.info(
        "Classification result for '%s': type=%s (%.2f), bank=%s (%.2f)",
        original_filename, doc_type, type_conf,
        bank_name or "unknown", bank_conf,
    )

    return {
        "document_type": doc_type,
        "type_confidence": type_conf,
        "bank_name": bank_name,
        "bank_confidence": bank_conf,
    }


def _llm_classify(
    filename: str,
    content: str,
) -> tuple[str, float] | None:
    """Attempt LLM-based classification as a last resort."""
    try:
        from openai import OpenAI, AzureOpenAI

        client = None
        model = None

        if os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"):
            client = AzureOpenAI(
                api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
                azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
            )
            model = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
        elif os.environ.get("OPENAI_API_KEY"):
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            model = "gpt-4o-mini"

        if client and content:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a document classifier. Respond ONLY with "
                            f"EXACTLY ONE of these types: {', '.join(DOCUMENT_TYPES)}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Filename: {filename}\n\nContent:\n{content[:2000]}",
                    },
                ],
                temperature=0.0,
                max_tokens=20,
            )
            result = response.choices[0].message.content.strip().lower()
            for valid_type in DOCUMENT_TYPES:
                if valid_type in result:
                    return valid_type, 0.85
    except ImportError:
        logger.debug("openai library not installed, skipping LLM classification")
    except Exception as exc:
        logger.warning("LLM classification failed: %s", exc)

    return None
