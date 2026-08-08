#!/usr/bin/env python3
"""Test API keys for LLM providers."""

import httpx
import sys

def test_cerebras(api_key: str) -> bool:
    """Test Cerebras API key."""
    print("Testing Cerebras...")
    try:
        resp = httpx.get(
            "https://api.cerebras.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            print(f"  ✅ Valid - {len(models)} models available")
            return True
        else:
            print(f"  ❌ Invalid - HTTP {resp.status_code}: {resp.json().get('message', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def test_google_ai(api_key: str) -> bool:
    """Test Google AI API key."""
    print("Testing Google AI...")
    try:
        resp = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            print(f"  ✅ Valid - {len(models)} models available")
            return True
        else:
            error = resp.json().get("error", {})
            print(f"  ❌ Invalid - HTTP {resp.status_code}: {error.get('message', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def main():
    # Load keys from .env file
    keys = {}
    try:
        with open(".env") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, value = line.strip().split("=", 1)
                    keys[key] = value
    except FileNotFoundError:
        print("Error: .env file not found")
        sys.exit(1)

    cerebras_key = keys.get("CEREBRAS_API_KEY", "")
    google_key = keys.get("GOOGLE_AI_API_KEY", "")

    if not cerebras_key:
        print("Warning: CEREBRAS_API_KEY not set in .env")
    if not google_key:
        print("Warning: GOOGLE_AI_API_KEY not set in .env")

    cerebras_ok = test_cerebras(cerebras_key) if cerebras_key else False
    google_ok = test_google_ai(google_key) if google_key else False

    print("\nSummary:")
    print(f"  Cerebras: {'✅' if cerebras_ok else '❌'}")
    print(f"  Google AI: {'✅' if google_ok else '❌'}")

    if not cerebras_ok or not google_ok:
        print("\nTo fix:")
        print("  1. Cerebras: Get key from https://cloud.cerebras.ai")
        print("  2. Google AI: Get key from https://aistudio.google.com/apikey")
        print("  3. Update .env file with valid keys")
        print("  4. Restart the server: systemctl --user restart llm-router-test")

if __name__ == "__main__":
    main()
