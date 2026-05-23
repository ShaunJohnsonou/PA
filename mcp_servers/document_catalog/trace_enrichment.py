"""TR-5.3 — Langfuse financial trace enrichment middleware.

Enriches Langfuse traces with financial-domain metadata (query type,
transaction counts, date coverage, validation warnings) so that the
observability dashboard surfaces domain-relevant information.

This module is **fail-open**: if the ``langfuse`` package is not installed
or any runtime error occurs, the function silently returns without raising.
Financial operations must never fail because of observability plumbing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def enrich_financial_trace(tool_name: str, metadata: dict) -> None:
    """Attach financial metadata to the current Langfuse trace.

    Expected *metadata* keys (all optional):

    - ``financial_query`` — the high-level query description
    - ``transaction_count`` — number of transactions processed
    - ``date_coverage_start`` — earliest date in the result set
    - ``date_coverage_end`` — latest date in the result set
    - ``validation_warnings`` — list of validation warning strings
    - ``computation_ms`` — wall-clock computation time in milliseconds

    Args:
        tool_name: Name of the MCP tool that produced the financial result.
        metadata: Dictionary of financial metadata to attach.
    """
    try:
        from langfuse import Langfuse  # noqa: F811
    except ImportError:
        logger.debug("langfuse package not available — skipping trace enrichment")
        return

    # Reason: All Langfuse interaction is wrapped in a blanket try/except
    # because this is best-effort enrichment. A failure here must never
    # propagate up and break the actual financial tool call.
    try:
        client = Langfuse()
        trace = client.trace(
            name=f"financial_{tool_name}",
            metadata={
                "tool_name": tool_name,
                **metadata,
            },
        )
        logger.debug(
            "Enriched Langfuse trace for %s (trace_id=%s)",
            tool_name,
            getattr(trace, "id", "unknown"),
        )
    except Exception:
        logger.debug(
            "Failed to enrich Langfuse trace for %s — continuing without enrichment",
            tool_name,
            exc_info=True,
        )
