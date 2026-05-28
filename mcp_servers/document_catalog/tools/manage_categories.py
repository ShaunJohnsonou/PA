"""``manage_categories`` MCP tool handler.

Allows the agent to list, create, and assign document categories.
Categories are life-area labels (finance, medicine, fitness, etc.)
that organise documents beyond their technical type.
"""
from __future__ import annotations

import logging
from typing import Any

from ..catalog_db import CatalogDB

logger = logging.getLogger(__name__)


async def handle_manage_categories(
    action: str,
    *,
    catalog: CatalogDB,
    category_name: str | None = None,
    category_description: str | None = None,
    document_id: str | None = None,
) -> dict:
    """Manage document categories.

    Actions:
        list       — return all available categories.
        create     — register a new category.
        assign     — assign a category to a document.
        documents  — list all documents in a category.

    Args:
        action: One of 'list', 'create', 'assign', 'documents'.
        catalog: CatalogDB instance.
        category_name: Required for create, assign, documents.
        category_description: Optional description for create.
        document_id: Required for assign.

    Returns:
        Dict with the action result.
    """
    action = (action or "").lower().strip()
    logger.info("manage_categories: action=%s, category=%s, doc=%s", action, category_name, document_id)

    if action == "list":
        categories = catalog.list_categories()
        return {
            "categories": categories,
            "count": len(categories),
            "message": f"{len(categories)} categories available",
        }

    elif action == "create":
        if not category_name:
            return _error("missing_name", "category_name is required for 'create' action")
        created = catalog.add_category(category_name, category_description or "")
        if created:
            return {
                "created": True,
                "category": category_name.lower().strip(),
                "message": f"Category '{category_name}' created successfully",
            }
        return {
            "created": False,
            "category": category_name.lower().strip(),
            "message": f"Category '{category_name}' already exists",
        }

    elif action == "assign":
        if not category_name:
            return _error("missing_name", "category_name is required for 'assign' action")
        if not document_id:
            return _error("missing_document_id", "document_id is required for 'assign' action")

        # Reason: verify the category exists. If not, auto-create it
        # so the agent can dynamically extend categories.
        categories = [c["name"] for c in catalog.list_categories()]
        clean_name = category_name.lower().strip()
        if clean_name not in categories:
            catalog.add_category(clean_name, f"Auto-created by agent")
            logger.info("Auto-created category '%s' for document %s", clean_name, document_id)

        catalog.update_document(document_id, category=clean_name)
        return {
            "assigned": True,
            "document_id": document_id,
            "category": clean_name,
            "message": f"Document {document_id} categorised as '{clean_name}'",
        }

    elif action == "documents":
        if not category_name:
            return _error("missing_name", "category_name is required for 'documents' action")
        docs = catalog.get_documents_by_category(category_name.lower().strip())
        return {
            "category": category_name.lower().strip(),
            "documents": [
                {
                    "document_id": d.document_id,
                    "filename": d.original_filename,
                    "document_type": d.document_type,
                    "upload_time": d.upload_time,
                }
                for d in docs
            ],
            "count": len(docs),
            "message": f"{len(docs)} documents in category '{category_name}'",
        }

    else:
        return _error(
            "invalid_action",
            f"Unknown action '{action}'. Valid actions: list, create, assign, documents",
        )


def _error(code: str, message: str) -> dict:
    logger.warning("manage_categories error [%s]: %s", code, message)
    return {"error": code, "message": message}
