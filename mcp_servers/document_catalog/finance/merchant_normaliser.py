"""Merchant name normaliser.

Cleans up raw transaction descriptions into standardised merchant
names. Uses regex patterns for common SA merchants and an LLM
fallback for unrecognised ones.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Reason: common SA merchant patterns mapped to clean names.
# These cover the majority of POS and debit-order descriptions.
_MERCHANT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pick\s*n\s*pay|pnp\b", re.I), "Pick n Pay"),
    (re.compile(r"checkers|shoprite", re.I), "Shoprite/Checkers"),
    (re.compile(r"woolworths|woolies", re.I), "Woolworths"),
    (re.compile(r"spar\b", re.I), "Spar"),
    (re.compile(r"dis[\s-]?chem", re.I), "Dis-Chem"),
    (re.compile(r"clicks\b", re.I), "Clicks"),
    (re.compile(r"game\b(?!.*game)", re.I), "Game"),
    (re.compile(r"makro\b", re.I), "Makro"),
    (re.compile(r"mr\s*price", re.I), "Mr Price"),
    (re.compile(r"takealot", re.I), "Takealot"),
    (re.compile(r"uber\s*eats", re.I), "Uber Eats"),
    (re.compile(r"uber\b(?!.*eats)", re.I), "Uber"),
    (re.compile(r"bolt\b", re.I), "Bolt"),
    (re.compile(r"netflix", re.I), "Netflix"),
    (re.compile(r"spotify", re.I), "Spotify"),
    (re.compile(r"amazon|amzn", re.I), "Amazon"),
    (re.compile(r"google\s*\*", re.I), "Google"),
    (re.compile(r"apple\.com|itunes", re.I), "Apple"),
    (re.compile(r"mcdonald", re.I), "McDonald's"),
    (re.compile(r"kfc\b", re.I), "KFC"),
    (re.compile(r"nando", re.I), "Nando's"),
    (re.compile(r"steers\b", re.I), "Steers"),
    (re.compile(r"engen\b", re.I), "Engen"),
    (re.compile(r"sasol\b", re.I), "Sasol"),
    (re.compile(r"shell\b", re.I), "Shell"),
    (re.compile(r"bp\b", re.I), "BP"),
    (re.compile(r"total\s*energies|totalenergies", re.I), "TotalEnergies"),
    (re.compile(r"discovery\s*(?:health|life|insure|vitality)", re.I), "Discovery"),
    (re.compile(r"old\s*mutual", re.I), "Old Mutual"),
    (re.compile(r"sanlam\b", re.I), "Sanlam"),
    (re.compile(r"momentum\b", re.I), "Momentum"),
    (re.compile(r"vodacom|vodafone", re.I), "Vodacom"),
    (re.compile(r"mtn\b", re.I), "MTN"),
    (re.compile(r"telkom\b", re.I), "Telkom"),
    (re.compile(r"rain\b(?!.*rain)", re.I), "Rain"),
    (re.compile(r"multichoice|dstv", re.I), "MultiChoice/DStv"),
    (re.compile(r"eskom\b", re.I), "Eskom"),
    (re.compile(r"city\s*(?:of|power)", re.I), "City Power"),
    # Bank fees
    (re.compile(r"monthly\s*(?:account|service)\s*fee", re.I), "Bank Fee"),
    (re.compile(r"card\s*fee|annual\s*card", re.I), "Card Fee"),
    (re.compile(r"atm\s*(?:withdrawal|cash)", re.I), "ATM Withdrawal"),
    (re.compile(r"pos\s*purchase", re.I), "POS Purchase"),
    # Transfers
    (re.compile(r"salary|payroll", re.I), "Salary"),
    (re.compile(r"transfer\s*(?:to|from)", re.I), "Transfer"),
]


def normalise_merchant(description_raw: str) -> str | None:
    """Attempt to normalise a raw transaction description into a merchant name.

    Returns the normalised merchant name, or None if no match found.
    """
    if not description_raw:
        return None

    for pattern, merchant in _MERCHANT_PATTERNS:
        if pattern.search(description_raw):
            logger.debug("normalise_merchant: '%s' -> %s", description_raw[:60], merchant)
            return merchant

    logger.debug("normalise_merchant: '%s' -> None (no match)", description_raw[:60])
    return None


async def normalise_merchant_llm(description_raw: str) -> str | None:
    """Use LLM to extract a merchant name from a raw description.

    Only called when regex-based normalisation fails.
    Returns the merchant name or None.
    """
    logger.debug("normalise_merchant_llm: input='%s'", description_raw[:80])
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
            return None

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the merchant or payee name from this bank "
                        "transaction description. Return ONLY the clean merchant "
                        "name (e.g. 'Pick n Pay', 'Uber Eats'). If you cannot "
                        "determine the merchant, return 'Unknown'."
                    ),
                },
                {"role": "user", "content": description_raw},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        result = response.choices[0].message.content.strip()
        logger.debug("normalise_merchant_llm: '%s' -> '%s'", description_raw[:40], result)
        return result if result and result.lower() != "unknown" else None

    except Exception as exc:
        logger.warning("LLM merchant normalisation failed: %s", exc)
        return None
