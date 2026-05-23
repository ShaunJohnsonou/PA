"""Transaction category classifier (FR-4.2, step 8).

Classifies transactions into spending categories using regex
heuristics first, then LLM structured output as fallback.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

CATEGORIES = [
    "groceries", "rent", "salary", "bank_fees", "insurance",
    "entertainment", "transport", "utilities", "medical",
    "education", "transfers", "fuel", "dining", "subscriptions",
    "clothing", "home", "other",
]

# Reason: fast heuristic classification covers ~70% of transactions
_CATEGORY_RULES: list[tuple[re.Pattern, str]] = [
    # Groceries
    (re.compile(r"pick\s*n\s*pay|pnp|checkers|shoprite|woolworths|spar|food\s*lover", re.I), "groceries"),
    # Fuel
    (re.compile(r"engen|sasol|shell|bp\b|caltex|total\s*energies|fuel|petrol", re.I), "fuel"),
    # Dining
    (re.compile(r"uber\s*eats|mr\s*d|mcdonald|kfc|nando|steers|spur|ocean\s*basket|wimpy", re.I), "dining"),
    # Transport
    (re.compile(r"uber(?!\s*eats)|bolt|gautrain|e-?toll", re.I), "transport"),
    # Subscriptions
    (re.compile(r"netflix|spotify|apple\.com|itunes|google\s*\*|youtube|amazon\s*prime|showmax|dstv|multichoice", re.I), "subscriptions"),
    # Insurance
    (re.compile(r"discovery|old\s*mutual|sanlam|momentum|hollard|outsurance|1st\s*for\s*women", re.I), "insurance"),
    # Utilities
    (re.compile(r"eskom|city\s*power|rand\s*water|joburg\s*water|municipality|rates|electricity", re.I), "utilities"),
    # Telecom (under utilities)
    (re.compile(r"vodacom|mtn|telkom|rain|cell\s*c|fibre", re.I), "utilities"),
    # Medical
    (re.compile(r"dis[\s-]?chem|clicks\s*pharmacy|medical|doctor|dr\b|hospital|pharmacy|pathcare|ampath", re.I), "medical"),
    # Bank fees
    (re.compile(r"monthly\s*(?:account|service)\s*fee|card\s*fee|bank\s*charge|admin\s*fee|atm\s*fee", re.I), "bank_fees"),
    # Rent
    (re.compile(r"rent|lease|landlord|property\s*management", re.I), "rent"),
    # Salary
    (re.compile(r"salary|payroll|wage|remuneration", re.I), "salary"),
    # Transfers
    (re.compile(r"transfer|payment\s*(?:to|from)|eft|ibt|inter[\s-]?account", re.I), "transfers"),
    # Education
    (re.compile(r"school|university|tuition|fees|college|wits|uct|unisa", re.I), "education"),
    # Clothing
    (re.compile(r"mr\s*price|edgars|jet\b|ackermans|truworths|foschini|h&m|zara|cotton\s*on", re.I), "clothing"),
]


def classify_transaction(
    description: str,
    merchant: str | None = None,
) -> tuple[str, float]:
    """Classify a transaction into a category.

    Returns (category, confidence).
    """
    text = f"{description} {merchant or ''}"

    for pattern, category in _CATEGORY_RULES:
        if pattern.search(text):
            logger.debug("classify_transaction: '%s' -> %s (regex, 0.85)", description[:60], category)
            return category, 0.85

    logger.debug("classify_transaction: '%s' -> other (no match, 0.3)", description[:60])
    return "other", 0.3


async def classify_transactions_llm(
    descriptions: list[str],
) -> list[tuple[str, float]]:
    """Batch-classify transactions using LLM structured output.

    Args:
        descriptions: List of transaction descriptions.

    Returns:
        List of (category, confidence) tuples.
    """
    if not descriptions:
        return []

    logger.info(
        "classify_transactions_llm: batch of %d descriptions",
        len(descriptions),
    )

    try:
        from openai import AzureOpenAI, OpenAI

        client = None
        model = None

        if os.environ.get("AZURE_API_KEY") and os.environ.get("AZURE_API_BASE"):
            client = AzureOpenAI(
                api_key=os.environ["AZURE_API_KEY"],
                api_version=os.environ.get("AZURE_API_VERSION", "2024-02-15-preview"),
                azure_endpoint=os.environ["AZURE_API_BASE"],
            )
            model = os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", "gpt-4o-mini")
        elif os.environ.get("OPENAI_API_KEY"):
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            model = "gpt-4o-mini"

        if client is None:
            return [("other", 0.3)] * len(descriptions)

        # Batch up to 50 descriptions per LLM call
        categories_str = ", ".join(CATEGORIES)
        numbered = "\n".join(f"{i+1}. {d}" for i, d in enumerate(descriptions[:50]))

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Classify each bank transaction into one of these categories: "
                        f"{categories_str}. "
                        "Respond with ONLY a numbered list matching the input, "
                        "with just the category name on each line."
                    ),
                },
                {"role": "user", "content": numbered},
            ],
            temperature=0.0,
            max_tokens=500,
        )

        result_text = response.choices[0].message.content.strip()
        lines = result_text.strip().split("\n")

        results = []
        for line in lines:
            # Remove numbering
            clean = re.sub(r"^\d+[\.\)]\s*", "", line).strip().lower()
            if clean in CATEGORIES:
                results.append((clean, 0.8))
            else:
                results.append(("other", 0.4))

        # Pad if LLM returned fewer lines
        while len(results) < len(descriptions):
            results.append(("other", 0.3))

        return results[:len(descriptions)]

    except Exception as exc:
        logger.warning("LLM categorisation failed: %s", exc)
        return [("other", 0.3)] * len(descriptions)
