"""
Quick test script for Azure OpenAI credentials.
Usage: python scripts/test_azure.py
Reads from .env file in the project root, or you can set env vars directly.
"""

import os
import json
import urllib.request
import urllib.error

# ── Try to load from .env file ──
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# ── Read credentials ──
API_KEY = os.environ.get("AZURE_API_KEY", "")
API_BASE = os.environ.get("AZURE_API_BASE", "").rstrip("/")
API_VERSION = os.environ.get("AZURE_API_VERSION", "2024-12-01-preview")
DEPLOYMENT = "gpt-5.4-nano"  # Change this if your deployment name differs
print(API_KEY)
print(API_BASE)
print(API_VERSION)
print(DEPLOYMENT)
if not API_KEY or not API_BASE:
    print("❌ Missing AZURE_API_KEY or AZURE_API_BASE in .env or environment.")
    exit(1)

# ── Build the request ──
url = f"{API_BASE}/openai/deployments/{DEPLOYMENT}/chat/completions?api-version={API_VERSION}"

payload = json.dumps({
    "messages": [{"role": "user", "content": "Say hello in one sentence."}]
}).encode("utf-8")

headers = {
    "Content-Type": "application/json",
    "api-key": API_KEY,
}

print(f"🔌 Endpoint:   {API_BASE}")
print(f"📦 Deployment: {DEPLOYMENT}")
print(f"📋 API Version: {API_VERSION}")
print(f"🌐 Full URL:   {url}")
print()

# ── Send the request ──
try:
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        reply = body["choices"][0]["message"]["content"]
        model = body.get("model", "unknown")
        print(f"✅ SUCCESS! Model responded: {reply}")
        print(f"   Model: {model}")
        print(f"   Tokens used: {body.get('usage', {})}")
except urllib.error.HTTPError as e:
    error_body = e.read().decode("utf-8", errors="replace")
    print(f"❌ HTTP {e.code} Error:")
    print(f"   {error_body}")
except Exception as e:
    print(f"❌ Connection Error: {e}")
