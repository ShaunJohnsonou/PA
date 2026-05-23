"""Final verification: plugin _get_langfuse init + trace."""
import os, sys
sys.path.insert(0, "/opt/hermes")
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/.hermes/.env"), override=True)

import plugins.observability.langfuse as lf_mod
# Reset cached state
lf_mod._LANGFUSE_CLIENT = None

client = lf_mod._get_langfuse()
if client is None or client is lf_mod._INIT_FAILED:
    print("FAIL: client =", client)
else:
    print("OK: client initialized")
    print("Auth:", client.auth_check())
    client.trace(name="final-verify", input="works", output="yes!")
    client.flush()
    print("Trace flushed successfully")
