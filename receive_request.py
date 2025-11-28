# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "python-dotenv", "httpx", "beautifulsoup4"]

import os
import json
import base64
import re
import asyncio
import traceback

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# -----------------------------------------------------
# Load environment variables
# -----------------------------------------------------
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")
AIPIPE_URL = "https://aipipe.org/openrouter/v1/chat/completions"
LLM_MODEL = "openai/gpt-4.1-nano"

if not SECRET_KEY or not AIPIPE_TOKEN:
    print("❌ ERROR: SECRET_KEY or AIPIPE_TOKEN missing in environment")

# Global FastAPI app
app = FastAPI()

# Global async http client
client = httpx.AsyncClient(timeout=60)


# -----------------------------------------------------
# Helper: Call AIPipe to answer the question
# -----------------------------------------------------
async def ask_llm(question_text: str) -> str:
    """
    Sends the question to AIPipe LLM and returns answer text.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "You answer data analysis quiz questions accurately."},
            {"role": "user", "content": question_text},
        ],
        "max_tokens": 500,
        "temperature": 0.0
    }

    headers = {
        "Authorization": f"Bearer {AIPIPE_TOKEN}",
        "Content-Type": "application/json"
    }

    resp = await client.post(AIPIPE_URL, headers=headers, json=payload)
    resp.raise_for_status()

    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# -----------------------------------------------------
# Core quiz solver loop
# -----------------------------------------------------
async def solve_quiz_chain(start_url: str, email: str, secret: str):
    """
    Fetch page → extract question → LLM → submit → repeat.
    Continues until server stops returning new URLs.
    """

    url = start_url

    while True:
        print(f"\n--- Fetching Quiz Page: {url}")

        # 1. Fetch quiz page
        r = await client.get(url)
        page_html = r.text

        # 2. Extract base64 HTML inside atob(...)
        m = re.search(r'atob\("([^"]+)"\)', page_html)
        decoded_html = None

        if m:
            decoded_html = base64.b64decode(m.group(1)).decode("utf-8")
            soup = BeautifulSoup(decoded_html, "html.parser")
        else:
            soup = BeautifulSoup(page_html, "html.parser")

        # 3. Extract question text
        q = soup.find("div", {"id": "result"}) or soup.find("div", class_="question")
        if not q:
            print("❌ No question found")
            return {"error": "No question found"}

        question_text = q.get_text(strip=True)
        print("\nQUESTION:", question_text)

        # 4. Get submit URL
        m2 = re.search(r'https?://[^"]+/submit', page_html)
        if not m2:
            print("❌ Could not find submit_url")
            return {"error": "Submit URL not found"}

        submit_url = m2.group(0)
        print("SUBMIT URL:", submit_url)

        # 5. Ask LLM for the answer
        print("\nAsking LLM for answer...")
        answer = await ask_llm(question_text)
        print("LLM ANSWER:", answer)

        # 6. Submit answer
        payload = {
            "email": email,
            "secret": secret,
            "url": url,
            "answer": answer
        }

        print("Submitting answer:", payload)
        resp = await client.post(submit_url, json=payload)

        # If not JSON → quiz ended
        try:
            result = resp.json()
        except:
            print("\nRAW RESPONSE (end):")
            print(resp.text)
            return {"final": True, "raw_response": resp.text}

        print("SERVER RESPONSE:", result)

        # If chain ended
        if "url" not in result:
            print("\n🎉 QUIZ COMPLETED")
            return result

        # Continue chain
        url = result["url"]


# -----------------------------------------------------
# Background Task
# -----------------------------------------------------
async def process_request(data):
    try:
        email = data["email"]
        secret = data["secret"]
        url = data["url"]

        print("\n====================================")
        print("PROCESS REQUEST START")
        print("Email:", email)
        print("URL:", url)
        print("====================================")

        result = await solve_quiz_chain(url, email, secret)

        print("\n===== FINAL RESULT =====")
        print(result)
        print("========================")

    except Exception as e:
        print("\n❌ PROCESS_REQUEST ERROR:", e)
        print(traceback.format_exc())


# -----------------------------------------------------
# HTTP API Endpoint
# -----------------------------------------------------
@app.post("/receive_request")
async def receive_request(request: Request, background_tasks: BackgroundTasks):

    try:
        data = await request.json()
    except:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # 1. Validate secret
    if data.get("secret") != SECRET_KEY:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    # 2. Validate required fields
    if not data.get("url") or not data.get("email"):
        return JSONResponse({"error": "Missing url or email"}, status_code=400)

    # 3. Run in background
    background_tasks.add_task(process_request, data)

    return {"message": "Request accepted"}


@app.get("/")
async def root():
    return {"status": "running", "endpoint": "/receive_request"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


# -----------------------------------------------------
# Run server (Railway uses $PORT)
# -----------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("receive_request:app", host="0.0.0.0", port=port)
