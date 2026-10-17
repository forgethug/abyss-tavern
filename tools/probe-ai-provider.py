import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPOSITORY_ROOT / ".env")


def main() -> int:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    api_base = os.getenv("DEEPSEEK_API_BASE", "").strip().rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
    if not api_key or not api_base:
        print(
            json.dumps(
                {
                    "ok": False,
                    "provider": "deepseek",
                    "reason": "missing provider configuration",
                }
            )
        )
        return 2

    endpoint = (
        api_base
        if api_base.endswith("/chat/completions")
        else f"{api_base}/chat/completions"
    )
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Return the single word OK.",
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
        },
        timeout=60,
    )
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        payload = {}

    if response.status_code >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        print(
            json.dumps(
                {
                    "ok": False,
                    "provider": "deepseek",
                    "model": model,
                    "status": response.status_code,
                    "error_type": (
                        error.get("type")
                        if isinstance(error, dict)
                        else None
                    ),
                    "error_code": (
                        error.get("code")
                        if isinstance(error, dict)
                        else None
                    ),
                }
            )
        )
        return 1

    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    print(
        json.dumps(
            {
                "ok": True,
                "provider": "deepseek",
                "model": model,
                "status": response.status_code,
                "response_id_present": bool(payload.get("id")),
                "choice_count": len(choices),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
