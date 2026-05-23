#!/usr/bin/env python3
"""
Simple script to view the contents of the Hermes Document Catalog SQLite Database.
Run this script from the root of your PA repository:
    python scripts/view_catalog.py
"""
import sqlite3
import os
from pathlib import Path
from datetime import datetime

# Assuming the script is run from the project root or scripts folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "vault" / "hermes_catalog.sqlite"

def format_row(row):
    """Format a single document row into a clean string."""
    doc_id = row['document_id']
    filename = row['original_filename']
    extract_status = row['extraction_status']
    doc_type = row['document_type']

    # Reason: indexing_status may not exist on older v1 databases
    try:
        index_status = row['indexing_status'] or 'pending'
    except (IndexError, KeyError):
        index_status = 'n/a'
    
    # Parse timestamps for better readability if they exist
    upload_time = row['upload_time']
    try:
        if upload_time:
            upload_time = upload_time.split('.')[0] # Remove microsecond precision
    except:
        pass

    return (
        f"ID: {doc_id[:8]}... | Type: {doc_type:<15} | "
        f"Extract: {extract_status:<10} | Index: {index_status:<10} | "
        f"File: {filename:<30} | Uploaded: {upload_time}"
    )


def main():
    if not DB_PATH.exists():
        print(f"[X] Database not found at: {DB_PATH}")
        print("Make sure you have ingested at least one document.")
        return

    print(f"[*] Opening catalog database: {DB_PATH}")
    print("-" * 80)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Fetch all documents
        cursor.execute("SELECT * FROM documents ORDER BY upload_time DESC")
        documents = cursor.fetchall()
        
        if not documents:
            print("The document catalog is completely empty. No files ingested yet.")
            return

        print(f"Total Documents: {len(documents)}\n")
        
        for doc in documents:
            print(format_row(doc))
            # Fetch tags for this document
            import json
            raw_tags = doc['tags']
            if raw_tags:
                try:
                    tags = json.loads(raw_tags)
                    if tags:
                        print(f"   Tags: {', '.join(tags)}")
                except:
                    print(f"   Tags: {raw_tags}")
    except sqlite3.OperationalError as e:
        print(f"[X] SQLite Error: {e}")
        print("The database might not be initialized properly yet.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
