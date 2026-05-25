import asyncio
import os
import sys

sys.path.insert(0, "./mcp_servers")

from document_catalog.server import handle_import_transactions

from document_catalog.vault import VaultManager
from document_catalog.catalog_db import CatalogDB
from document_catalog.finance.ledger_db import FinanceLedger

vault = VaultManager("C:/Users/ShaunJohnson/repos/PA/vault")
catalog = CatalogDB("C:/Users/ShaunJohnson/repos/PA/mcp_servers/document_catalog/catalog.db")
ledger = FinanceLedger("C:/Users/ShaunJohnson/repos/PA/mcp_servers/document_catalog/ledger.db")

async def main():
    doc_id = "3d0bccfa-7fde-4a9b-bf8a-e89474588554"
    res = await handle_import_transactions(doc_id, None, catalog, vault, ledger)
    print("Result:", res)

asyncio.run(main())
