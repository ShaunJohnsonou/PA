#!/usr/bin/env python3
"""
Debug script to test FAISS hybrid search locally inside the container.
Usage: python scripts/debug_search.py "your search query"
"""
import sys
import os
import asyncio
from pathlib import Path
import dotenv

def load_env():
    # Attempt to load from multiple potential locations
    paths = [
        Path("/opt/hermes/.env")
    ]
    for p in paths:
        if p.exists():
            print(f"[*] Loaded .env from {p}")
            dotenv.load_dotenv(p)
            break

async def main():
    load_env()
    
    if len(sys.argv) < 2:
        print("Usage: python debug_search.py '<query>'")
        return

    query = sys.argv[1]
    
    # Imports specific to our MCP server
    from mcp_servers.document_catalog.catalog_db import CatalogDB
    from mcp_servers.document_catalog.search.faiss_index import FaissIndexManager
    from mcp_servers.document_catalog.search.fts_search import FTS5Search
    from mcp_servers.document_catalog.search.embeddings import EmbeddingService
    
    # 1. Initialize paths and components
    vault_dir = Path(os.environ.get("HERMES_VAULT_PATH", "/hermes-vault"))
    print(f"[*] Vault Path: {vault_dir}")
    
    db_path = vault_dir / "hermes_catalog.sqlite"
    db = CatalogDB(db_path)
    print(f"[*] Database loaded. Documents: {len(db.list_documents())}")

    fts = FTS5Search(db_path)
    faiss = FaissIndexManager(vault_dir)
    faiss.load_or_create()
    embeddings = EmbeddingService()
    
    print(f"[*] Emdeddings Available: {embeddings.available}")
    print(f"[*] FAISS Index loaded. Total vectors: {faiss.size}")

    print(f"\n--- SEARCHING FOR: '{query}' ---")
    
    # 2. Test FTS5 alone
    print("\n[ FTS5 KEYWORD SEARCH ]")
    fts_results = fts.search(query, top_k=5)
    print(f"Found {len(fts_results)} FTS5 results.")
    for hit in fts_results:
        print(f"  - [{hit.rank:.4f}] {hit.document_id[:8]} Chunk {hit.chunk_index}: {hit.text[:50]}...")

    # 3. Test Embeddings + FAISS
    if not embeddings.available:
        print("\n[X] Embedding service unavailable, cannot test semantic search.")
        return

    print("\n[ GENERATING QUERY EMBEDDING ]")
    try:
        query_vec = await embeddings.embed_query(query)
        if query_vec is None:
            print("[X] Failed to generate embedding for query.")
            return
        print(f"[*] Query embedded successfully (dim: {len(query_vec)})")
    except Exception as e:
        print(f"[X] Embedding generation crashed: {e}")
        return

    print("\n[ FAISS SEMANTIC SEARCH ]")
    try:
        faiss_results = faiss.search(query_vec, top_k=5)
        print(f"Found {len(faiss_results)} FAISS results.")
        for res in faiss_results:
            chunk_id = res["chunk_id"]
            dist = res["score"]
            # Need to look up chunk metadata
            chunk = fts.get_chunk_by_id(chunk_id)
            if chunk:
                print(f"  - [{dist:.4f}] {chunk.document_id[:8]} Chunk {chunk.chunk_index}: {chunk.text[:50]}...")
            else:
                print(f"  - [{dist:.4f}] {chunk_id} (CHUNK METADATA MISSING IN SQLITE!)")
    except Exception as e:
        print(f"[X] FAISS search crashed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
