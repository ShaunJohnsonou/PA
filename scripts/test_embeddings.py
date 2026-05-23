#!/usr/bin/env python3
"""
Quick test script for the Azure OpenAI embedding model.
Verifies that credentials, deployment name, and API connectivity are working.

Usage (from project root):
    python scripts/test_embeddings.py

Or inside the Docker container:
    /opt/hermes/.venv/bin/python /opt/hermes/scripts/test_embeddings.py
"""
import os
import sys
import time
import dotenv
dotenv.load_dotenv()


def main():
    # ── 1. Check environment variables ───────────────────────────
    api_key = os.environ.get("AZURE_API_KEY", "")
    api_base = os.environ.get(
        "AZURE_EMBEDDING_API_BASE", 
        os.environ.get("AZURE_API_BASE", "")
    )
    api_version = os.environ.get("AZURE_API_VERSION", "2024-12-01-preview")
    deployment = os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")

    print("=" * 60)
    print("Azure OpenAI Embedding Test")
    print("=" * 60)
    print(f"  Endpoint:   {api_base or '(not set)'}")
    print(f"  API Key:    {'***' + api_key[-4:] if len(api_key) > 4 else '(not set)'}")
    print(f"  API Version:{api_version}")
    print(f"  Deployment: {deployment}")
    print("-" * 60)

    if not api_key or not api_base:
        print("[X] AZURE_API_KEY or AZURE_API_BASE not set.")
        print("    Set these in your .env file or export them:")
        print("      export AZURE_API_KEY=your-key-here")
        print("      export AZURE_API_BASE=https://your-resource.openai.azure.com")
        sys.exit(1)

    # ── 2. Import and initialise client ──────────────────────────
    try:
        from openai import AzureOpenAI
    except ImportError:
        print("[X] openai package not installed. Run: pip install openai")
        sys.exit(1)

    client = AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=api_base,
    )
    print("[OK] AzureOpenAI client initialised")

    # ── 3. Test with sample texts ────────────────────────────────
    test_texts = [
        "The property transfer was registered on 15 March 2025.",
        "Monthly rental payment of R12,500 is due on the first of each month.",
        "This is a completely unrelated sentence about cooking pasta.",
    ]

    print(f"\n[*] Embedding {len(test_texts)} test texts...")
    t0 = time.time()

    try:
        response = client.embeddings.create(
            input=test_texts,
            model=deployment,
        )
    except Exception as exc:
        print(f"[X] API call failed: {exc}")
        print("\n    Common causes:")
        print("    - Deployment name mismatch (check Azure OpenAI Studio)")
        print("    - API key expired or incorrect")
        print("    - Model not deployed in your region")
        sys.exit(1)

    elapsed = time.time() - t0
    print(f"[OK] API call succeeded in {elapsed:.2f}s")

    # ── 4. Inspect results ───────────────────────────────────────
    print(f"\n{'Text':<55} | Dim   | First 5 values")
    print("-" * 100)

    vectors = []
    for i, item in enumerate(response.data):
        vec = item.embedding
        preview = ", ".join(f"{v:.4f}" for v in vec[:5])
        label = test_texts[i][:52] + "..." if len(test_texts[i]) > 52 else test_texts[i]
        print(f"{label:<55} | {len(vec):<5} | [{preview}]")
        vectors.append(vec)

    # ── 5. Compute cosine similarity ─────────────────────────────
    print("\n[*] Cosine similarity matrix:")
    print(f"    {'':>5}", end="")
    for i in range(len(test_texts)):
        print(f"  Text {i+1}", end="")
    print()

    for i in range(len(vectors)):
        print(f"    Text {i+1}", end="")
        for j in range(len(vectors)):
            sim = _cosine_similarity(vectors[i], vectors[j])
            print(f"  {sim:.4f}", end="")
        print()

    # ── 6. Summary ───────────────────────────────────────────────
    sim_01 = _cosine_similarity(vectors[0], vectors[1])
    sim_02 = _cosine_similarity(vectors[0], vectors[2])

    print(f"\n{'=' * 60}")
    print(f"[OK] Embedding model '{deployment}' is working correctly!")
    print(f"     Dimension: {len(vectors[0])}")
    print(f"     Property vs Rental similarity: {sim_01:.4f} (should be moderate)")
    print(f"     Property vs Cooking similarity: {sim_02:.4f} (should be low)")

    if sim_01 > sim_02:
        print("     [PASS] Related texts are more similar than unrelated ones")
    else:
        print("     [WARN] Similarity ranking unexpected - check model quality")

    print(f"     Latency: {elapsed:.2f}s for {len(test_texts)} texts")
    print(f"{'=' * 60}")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


if __name__ == "__main__":
    main()
