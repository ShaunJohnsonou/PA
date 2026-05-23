"""Document type classifier.

Categorises documents into predefined types to route them
through the correct extraction and analysis pipelines.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Valid types as per FR-2.4
DOCUMENT_TYPES = {
    "bank_statement",
    "invoice",
    "payslip",
    "tax_document",
    "financial_report",
    "general"
}

def classify_document(
    original_filename: str, 
    first_page_text: str = ""
) -> tuple[str, float]:
    """
    Classify a document based on its filename and first page content.
    Returns (document_type, confidence).
    """
    filename_lower = original_filename.lower()
    content_lower = first_page_text[:2000].lower() if first_page_text else ""
    
    # 1. Filename heuristics
    if "statement" in filename_lower or "estatement" in filename_lower:
        return "bank_statement", 0.8
    if "invoice" in filename_lower or "receipt" in filename_lower:
        return "invoice", 0.8
    if "payslip" in filename_lower or "salary" in filename_lower:
        return "payslip", 0.8
    if "irp5" in filename_lower or "it3" in filename_lower or "tax" in filename_lower:
        return "tax_document", 0.8
        
    # 2. Content heuristics
    if "statement of account" in content_lower or "bank statement" in content_lower:
        return "bank_statement", 0.7
    if "tax invoice" in content_lower or "invoice no" in content_lower:
        return "invoice", 0.7
    if "payslip" in content_lower or "earnings" in content_lower and "deductions" in content_lower:
        return "payslip", 0.7
    if "tax certificate" in content_lower:
        return "tax_document", 0.7
        
    # 3. LLM classification fallback
    try:
        from openai import OpenAI, AzureOpenAI
        
        client = None
        model = None
        
        # Prefer Azure OpenAI if configured
        if os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"):
            client = AzureOpenAI(
                api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
                azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT")
            )
            model = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
        # Fall back to standard OpenAI
        elif os.environ.get("OPENAI_API_KEY"):
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            model = "gpt-4o-mini"
            
        if client and content_lower:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system", 
                            "content": f"You are a document classifier. Respond ONLY with EXACTLY ONE of these types, and nothing else: {', '.join(DOCUMENT_TYPES)}"
                        },
                        {
                            "role": "user", 
                            "content": f"Filename: {original_filename}\n\nFirst page content:\n{first_page_text[:2000]}"
                        }
                    ],
                    temperature=0.0,
                    max_tokens=20
                )
                result = response.choices[0].message.content.strip().lower()
                
                # Verify the LLM returned a valid type
                for valid_type in DOCUMENT_TYPES:
                    if valid_type in result:
                        return valid_type, 0.9
            except Exception as e:
                logger.warning("LLM classification failed: %s", e)
    except ImportError:
        logger.debug("openai library not installed, skipping LLM classification fallback")
        pass
        
    # Default fallback
    return "general", 0.5
