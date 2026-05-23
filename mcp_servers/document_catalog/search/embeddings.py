"""Azure OpenAI embedding service.

Generates dense vector embeddings for text chunks via the Azure OpenAI
embeddings API. Handles batching, retries, and cost tracking.
"""
from __future__ import annotations

import logging
import os
import time

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generate embeddings via Azure OpenAI with batching and retry.

    Uses the text-embedding-3-large model (3072 dimensions) deployed
    on the user's Azure OpenAI resource.
    """

    DEFAULT_DEPLOYMENT = "text-embedding-3-large"
    DEFAULT_DIM = 3072
    MAX_BATCH_SIZE = 100

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        api_version: str | None = None,
        deployment: str | None = None,
    ):
        """
        Args:
            api_key: Azure OpenAI API key. Falls back to AZURE_API_KEY env var.
            api_base: Azure endpoint URL. Falls back to AZURE_API_BASE env var.
            api_version: Azure API version. Falls back to AZURE_API_VERSION env var.
            deployment: Azure deployment name. Falls back to AZURE_EMBEDDING_DEPLOYMENT env var.
        """
        self._api_key = api_key or os.environ.get("AZURE_API_KEY", "")
        
        # Reason: Use a dedicated env var for the embedding base URL to avoid 
        # conflicts with the LLM routing gateway which might require /openai/v1 paths.
        self._api_base = api_base or os.environ.get(
            "AZURE_EMBEDDING_API_BASE", 
            os.environ.get("AZURE_API_BASE", "")
        )

        self._api_version = api_version or os.environ.get("AZURE_API_VERSION", "2024-12-01-preview")
        self._deployment = deployment or os.environ.get(
            "AZURE_EMBEDDING_DEPLOYMENT", self.DEFAULT_DEPLOYMENT
        )
        self._dimension = self.DEFAULT_DIM
        self._client = None
        self._available = bool(self._api_key and self._api_base)

        if not self._available:
            logger.warning(
                "Azure OpenAI credentials not set (AZURE_API_KEY / AZURE_API_BASE). "
                "Embedding generation disabled — documents will only be keyword-searchable."
            )

    @property
    def available(self) -> bool:
        """True if the embedding service has valid credentials."""
        return self._available

    @property
    def dimension(self) -> int:
        """Vector dimensionality of the embedding model."""
        return self._dimension

    @property
    def model_name(self) -> str:
        """Name of the embedding deployment."""
        return self._deployment

    def _get_client(self):
        """Lazy-initialise the Azure OpenAI client."""
        if self._client is None:
            try:
                # Reason: Try Langfuse drop-in replacement first so embedding token usage 
                # and latency are tracked natively in the Langfuse dashboard.
                try:
                    from langfuse.openai import AzureOpenAI
                    wrapper = "Langfuse-wrapped"
                except ImportError:
                    from openai import AzureOpenAI
                    wrapper = "Standard"
                
                self._client = AzureOpenAI(
                    api_key=self._api_key,
                    api_version=self._api_version,
                    azure_endpoint=self._api_base,
                )
                logger.info(
                    "%s Azure OpenAI client initialised (endpoint=%s, deployment=%s)",
                    wrapper, self._api_base, self._deployment,
                )
            except ImportError:
                raise RuntimeError(
                    "openai package is not installed. Run: pip install openai"
                )
        return self._client

    async def embed_chunks(self, chunks: list) -> list[np.ndarray] | None:
        """Generate embeddings for a list of ChunkRecord objects.

        Batches chunks into groups of MAX_BATCH_SIZE for efficient API calls.
        Retries failed batches up to 3 times with exponential backoff.

        Returns:
            List of numpy float32 arrays (one per chunk), or None if
            the service is unavailable.
        """
        if not self._available:
            logger.warning("Embedding service unavailable — skipping embedding generation")
            return None

        texts = [c.text for c in chunks]
        all_vectors: list[np.ndarray] = []

        # Process in batches
        for batch_start in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[batch_start:batch_start + self.MAX_BATCH_SIZE]
            vectors = await self._embed_batch_with_retry(batch)

            if vectors is None:
                logger.error("Embedding batch failed after retries — aborting")
                return None

            all_vectors.extend(vectors)

        # Update chunk metadata
        for chunk, vec in zip(chunks, all_vectors):
            chunk.embedding_model = self._deployment
            chunk.embedding_dim = self._dimension

        total_tokens = sum(c.token_count for c in chunks)
        logger.info(
            "Embedded %d chunks, ~%dK tokens, est. $%.4f",
            len(chunks),
            total_tokens // 1000,
            self.estimate_cost(total_tokens),
        )

        return all_vectors

    async def embed_query(self, query: str) -> np.ndarray | None:
        """Embed a single query string for search.

        Returns:
            numpy float32 array of shape (dimension,), or None if unavailable.
        """
        if not self._available:
            return None

        vectors = await self._embed_batch_with_retry([query])
        if vectors and len(vectors) > 0:
            return vectors[0]
        return None

    async def _embed_batch_with_retry(
        self, texts: list[str], max_retries: int = 3
    ) -> list[np.ndarray] | None:
        """Embed a batch of texts with exponential backoff retry."""
        import asyncio

        client = self._get_client()
        backoff = 0.5

        for attempt in range(max_retries):
            try:
                response = client.embeddings.create(
                    input=texts,
                    model=self._deployment,
                )

                vectors = []
                for item in response.data:
                    vec = np.array(item.embedding, dtype=np.float32)
                    # Reason: L2-normalise so inner product = cosine similarity
                    norm = np.linalg.norm(vec)
                    if norm > 0:
                        vec = vec / norm
                    vectors.append(vec)

                return vectors

            except Exception as exc:
                logger.warning(
                    "Embedding API call failed (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2

        return None

    def estimate_cost(self, total_tokens: int) -> float:
        """Estimate API cost in USD for the given token count.

        text-embedding-3-large: $0.13 per 1M tokens.
        """
        return (total_tokens / 1_000_000) * 0.13
