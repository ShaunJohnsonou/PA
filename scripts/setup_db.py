#!/usr/bin/env python3
"""
setup_db.py — Initializes the SQLite database for the Personal Assistant.
Usage: python3 setup_db.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "pa_index.db")
INIT_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "init", "init_sqlite.sql")

def main():
    print(f"Initializing SQLite database at: {DB_PATH}")
    
    if not os.path.exists(INIT_SCRIPT):
        print(f"Error: Could not find initialization script at {INIT_SCRIPT}")
        return

    with open(INIT_SCRIPT, "r", encoding="utf-8") as f:
        schema = f.read()

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(schema)
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Failed to initialize database: {e}")

if __name__ == "__main__":
    main()
