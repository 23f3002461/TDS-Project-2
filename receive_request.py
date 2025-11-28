# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "python-dotenv", "httpx", "beautifulsoup4"]

import os
import re
import json
import base64
import asyncio
import traceback
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

# ---------------- Config ----------------
SECRET_KEY = os.getenv("SECRET_KEY")
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")
AIPIPE_URL = "https://aipipe.org/openrouter/v1/chat/completions"
LLM_MODEL = "openai/gpt-4.1-nano"  # change if needed

if not SECRET_KEY or not AIPIPE_TOKEN:
    print("WARNING: SECRET_KEY or AIPIPE_TOKEN not set in environment")

# ---------------- App & Client ----------------
app = FastAPI()
http_client = httpx.AsyncClient(timeout=60.0)

# ---------------- Helpers ----------------
def extract_base64_from_html(html: str) -> Optional[str]:
    m = re.search(r'atob\(\s*["\']([^"\']+)["\']\s*\)', html)
    return m.group(1) if m else None

def decode_base64_to_html(b64: str) -> Optional[str]:
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        return None

def extract_question_text(decoded_html: str) -> str:
    soup = BeautifulSoup(decoded_html, "html.parser")
    # prefer id="result"
    el = soup.find(id="result")
    if el and el.get_text(strip=True):
        return el.get_text(separator="\n", strip=True)
    # fallback to <pre>
    pre = soup.find("pre")
    if pre and pre.get_text(strip=True):
        return pre.get_text(separator="\n", strip=True)
    # fallback body text
    if soup.body:
        return soup.body.get_text(separator="\n", strip=True)
    return decoded_html.strip()

def find_submit_url_in_html(html: str, base_url: str) -> Optional[str]:
    # 1) absolute /submit
    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", html)
    if m:
        return m.group(0)
    # 2) JSON-like "url":"..."
    m2 = re.search(r'"url"\s*:\s*"([^"]+)"', html)
    if m2:
        candidate = m2.group(1).strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
        # join relative
        return urljoin(base_url, candidate if candidate.startswith("/") else "/" + candidate)
    # 3) fallback: first /submit path
    m3 = re.search(r'(/submit[^\s\'"<>]*)', html)
    if m3:
        return urljoin(base_url, m3.group(1))
    return None

def safe_json_load(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:
        return None

# ---------------- AIPipe call: LLM computes answer only ----------------
async def call_aipipe_for_answer(question_text: str, retries: int = 2, timeout: float = 30.0) -> Any:
    """
    Ask AIPipe to compute the answer for the question_text.
    Expect EXACTLY a JSON object: {"answer": <value>}
    Returns the answer value.
    """
    headers = {
        "Authorization": f"Bearer {AIPIPE_TOKEN}",
        "Content-Type": "application/json",
    }

    prompt = (
        "You are a strict assistant that reads a single plain-text quiz question and MUST RETURN ONLY a JSON object "
        "with exactly one key 'answer'. Do NOT include any other text, explanation, or markdown. "
        "The 'answer' value must be a number, string, boolean, or JSON object.\n\n"
        "QUESTION:\n"
        f"{question_text}\n\n"
        "Return only: {\"answer\": ... }"
    )

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Output only valid JSON with a single key named 'answer'."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.0,
    }

    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(AIPIPE_URL, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            j = resp.json()
            # support OpenRouter/chat shape
            content = None
            try:
                content = j["choices"][0]["message"]["content"]
            except Exception:
                content = j.get("choices", [{}])[0].get("text") if isinstance(j.get("choices"), list) else None

            if content is None:
                raise ValueError("No content in AIPipe response")

            text = content.strip()
            # strip fenced code if present
            m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
            json_text = m.group(1) if m else (re.search(r"(\{.*\})", text, re.DOTALL) or None)
            if isinstance(json_text, re.Match):
                json_text = json_text.group(1)
            json_text = json_text if isinstance(json_text, str) else (text if m else (text if json_text is None else text))

            parsed = safe_json_load(json_text)
            if parsed and "answer" in parsed:
                return parsed["answer"]

            # try direct primitive parse: number, boolean, bare value
            stripped = text.strip().strip('"').strip("'")
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped)
            if re.fullmatch(r"-?\d+\.\d+", stripped):
                return float(stripped)
            if stripped.lower() in ("true", "false"):
                return stripped.lower() == "true"

            raise ValueError(f"Could not parse answer JSON from LLM output: {text}")

        except Exception as e:
            last_exc = e
            print(f"AIPipe attempt {attempt} failed: {repr(e)}")
            if attempt <= retries:
                await asyncio.sleep(1.0 * attempt)
            else:
                break

    raise last_exc

# ---------------- Background worker ----------------
async def process_request(data: Dict[str, Any]):
    """
    SAFE MODE (Option 1):
    - fetch page, decode base64, extract question
    - ask LLM for answer (single-question)
    - post answer to submit_url
    - follow next URL until finished
    """
    try:
        start_url = data.get("url")
        email = data.get("email")
        secret = data.get("secret")

        if not start_url or not start_url.startswith(("http://", "https://")):
            print("Invalid start URL:", start_url)
            return

        overall_deadline = asyncio.get_event_loop().time() + 150  # seconds

        url = start_url
        last_result = None

        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                if asyncio.get_event_loop().time() > overall_deadline:
                    print("Deadline exceeded; aborting.")
                    break

                try:
                    resp = await client.get(url)
                except Exception as e:
                    print("GET failed for", url, repr(e))
                    break

                html = resp.text or ""
                b64 = extract_base64_from_html(html)
                decoded_html = decode_base64_to_html(b64) if b64 else None
                page_to_parse = decoded_html if decoded_html else html

                question_text = extract_question_text(page_to_parse)
                if not question_text:
                    print("No question text found on page:", url)
                    break

                submit_url = find_submit_url_in_html(page_to_parse, url)
                if not submit_url:
                    print("No submit URL found on page; printing snippet and aborting.")
                    print(page_to_parse[:500])
                    break

                # ask LLM for answer
                try:
                    answer = await call_aipipe_for_answer(question_text)
                except Exception as e:
                    print("LLM failed to compute answer:", repr(e))
                    break

                payload = {"email": email, "secret": secret, "url": url, "answer": answer}
                try:
                    post_resp = await client.post(submit_url, json=payload, timeout=60.0)
                except Exception as e:
                    print("POST to submit_url failed:", submit_url, repr(e))
                    break

                # parse post response leniently
                try:
                    post_json = post_resp.json()
                except Exception:
                    txt = (post_resp.text or "").strip()
                    print("Submit response was not JSON; treating as final:", txt[:200])
                    last_result = {"final": True, "raw_response": txt}
                    break

                print("Submit response:", post_json)
                last_result = post_json

                # if next URL given, continue
                next_url = None
                if isinstance(post_json, dict) and post_json.get("url"):
                    next_url = post_json["url"]

                if not next_url:
                    break

                if next_url == url:
                    print("Next URL same as current; aborting to avoid loop.")
                    break

                url = next_url

        print("Background task finished. Last result:", last_result)
        return

    except Exception:
        print("process_request unexpected error:\n", traceback.format_exc())
        return

# ---------------- API endpoints ----------------
@app.post("/receive_request")
async def receive_request(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    if data.get("secret") is None:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if data.get("secret") != SECRET_KEY:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not data.get("url") or not data.get("email"):
        return JSONResponse({"error": "Missing required fields (url/email)"}, status_code=400)

    background_tasks.add_task(process_request, data)
    return JSONResponse({"message": "Request accepted"}, status_code=200)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/")
async def root():
    return {"service": "IITM Quiz Solver (Option 1)", "endpoint": "/receive_request"}

# ---------------- cleanup ----------------
@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("receive_request:app", host="0.0.0.0", port=port)
