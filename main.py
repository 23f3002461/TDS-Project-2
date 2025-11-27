# ======================================================
#  LLM ANALYSIS QUIZ SOLVER – FINAL FULL VERSION
#  Fully supports:
#   - Multi-question windows (3 min per question)
#   - Retries on older questions
#   - Playwright rendering
#   - PDF / CSV / XLSX / HTML scraping
#   - Auto-follow next_url
#   - Server hosting (Flask)
# ======================================================

import os
import re
import time
import json
import logging
import requests
import pandas as pd
import pdfplumber
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ================
# CONFIG
# ================
EXPECTED_SECRET = os.environ.get("QUIZ_SECRET", "Mysecret")
HTTP_TIMEOUT = 30
MAX_GLOBAL_SECONDS = 170   # Always stay under 180 seconds

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)


# ======================================================
# HELPERS
# ======================================================

def find_submit_url_from_html(html_text, base_url):
    soup = BeautifulSoup(html_text, "lxml")

    # form action
    form = soup.find("form", action=True)
    if form:
        return urljoin(base_url, form["action"])

    # any suspicious URL in text
    urls = re.findall(r"https?://[^\s\"']+", html_text)
    for u in urls:
        if "submit" in u or "answer" in u:
            return urljoin(base_url, u)

    return urls[0] if urls else None


def post_answer(submit_url, payload):
    resp = requests.post(submit_url, json=payload, timeout=HTTP_TIMEOUT)
    try:
        return resp.status_code, resp.json()
    except:
        return resp.status_code, {"text": resp.text}


def download_file(url):
    local = "/tmp/llmquiz"
    os.makedirs(local, exist_ok=True)
    fname = os.path.join(local, os.path.basename(urlparse(url).path) or "file")
    r = requests.get(url, stream=True, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    with open(fname, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return fname


def parse_html_table_sum(html_text, colname="value"):
    soup = BeautifulSoup(html_text, "lxml")
    table = soup.find("table")
    if not table:
        return None

    df = pd.read_html(str(table))[0]

    if colname in df.columns:
        df[colname] = pd.to_numeric(df[colname].astype(str).str.replace(",", ""), errors="coerce")
        return float(df[colname].sum())

    # else try any numeric column
    for c in df.columns:
        try:
            vals = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
            if vals.notna().sum() > 0:
                return float(vals.sum())
        except:
            pass

    return None


def sum_values_in_pdf(pdf_path, page_number=2, colname="value"):
    with pdfplumber.open(pdf_path) as pdf:
        if page_number - 1 >= len(pdf.pages):
            return None

        page = pdf.pages[page_number - 1]
        tables = page.extract_tables()

        if not tables:
            # fallback: extract all numbers
            text = page.extract_text() or ""
            nums = re.findall(r"[-+]?\d*\.\d+|\d+", text.replace(",", ""))
            if nums:
                return sum(float(n) for n in nums)
            return None

        df = pd.DataFrame(tables[0][1:], columns=tables[0][0])

        if colname in df.columns:
            df[colname] = pd.to_numeric(df[colname].astype(str).str.replace(",", ""), errors="coerce")
            return float(df[colname].sum())

        # else try any numeric column
        for c in df.columns:
            try:
                vals = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
                return float(vals.sum())
            except:
                pass

    return None


# ======================================================
# HANDLERS (with confidence scores)
# ======================================================

HANDLERS = []

def handler(fn):
    HANDLERS.append(fn)
    return fn


@handler
def base64_json_handler(html, url, page):
    # detect base64 blocks
    m = re.search(r"atob\(`([^`]+)`\)", html)
    if not m:
        return None
    import base64
    try:
        txt = base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
        # find JSON
        jm = re.search(r"\{[\s\S]*?\}", txt)
        if jm:
            data = json.loads(jm.group(0))
            if "answer" in data:
                return {"answer": data["answer"], "confidence": 0.99}
    except:
        return None
    return None


@handler
def html_table_handler(html, url, page):
    # check if question mentions table sum
    if "sum" in html.lower() and "value" in html.lower():
        val = parse_html_table_sum(html, "value")
        if val is not None:
            return {"answer": val, "confidence": 0.92}
    return None


@handler
def download_and_parse_handler(html, url, page):
    soup = BeautifulSoup(html, "lxml")
    links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]

    for link in links:
        if any(ext in link.lower() for ext in [".csv", ".xlsx", ".xls", ".pdf"]):
            try:
                f = download_file(link)

                if f.endswith(".csv"):
                    df = pd.read_csv(f)
                    if "value" in df.columns:
                        s = df["value"].astype(str).str.replace(",", "").astype(float).sum()
                        return {"answer": float(s), "confidence": 0.9}
                    for c in df.columns:
                        try:
                            s = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce").sum()
                            return {"answer": float(s), "confidence": 0.7}
                        except:
                            pass

                if f.endswith(".xlsx") or f.endswith(".xls"):
                    df = pd.read_excel(f)
                    if "value" in df.columns:
                        s = pd.to_numeric(df["value"], errors="coerce").sum()
                        return {"answer": float(s), "confidence": 0.9}

                if f.endswith(".pdf"):
                    s = sum_values_in_pdf(f, 2, "value")
                    if s is not None:
                        return {"answer": float(s), "confidence": 0.9}

            except Exception as e:
                logging.error("Download parse error: %s", e)

    return None


@handler
def simple_regex_number(html, url, page):
    # fallback: extract simple "answer is 1234"
    m = re.search(r"answer\s*is\s*[:\s]*([0-9\.\,]+)", html, flags=re.I)
    if m:
        try:
            return {"answer": float(m.group(1).replace(",", "")), "confidence": 0.4}
        except:
            pass
    return None


# ======================================================
# MULTI-QUESTION MANAGER
# ======================================================

class QuestionManager:
    def __init__(self):
        self.questions = []  # each q: {url, start, deadline, solved, answer, confidence}

    def add(self, url):
        now = time.time()
        self.questions.append({
            "url": url,
            "start": now,
            "deadline": now + 180,   # 3 minutes
            "solved": False,
            "answer": None,
            "confidence": 0.0
        })

    def next_unsolved(self):
        now = time.time()
        active = [q for q in self.questions if not q["solved"] and now < q["deadline"]]
        if not active:
            return None
        active.sort(key=lambda x: x["deadline"])  # closest deadline first
        return active[0]


# ======================================================
# MAIN SOLVER
# ======================================================

def solve_quiz_url(start_url, email, secret):
    global_deadline = time.time() + MAX_GLOBAL_SECONDS

    manager = QuestionManager()
    manager.add(start_url)

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        while time.time() < global_deadline:
            q = manager.next_unsolved()
            if not q:
                break

            now = time.time()
            if now >= q["deadline"]:
                q["solved"] = True
                continue

            url = q["url"]

            # Render page
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                html = page.content()
            except:
                q["solved"] = True
                continue

            submit_url = find_submit_url_from_html(html, url)
            if not submit_url:
                q["solved"] = True
                continue

            # Run handlers
            answer = None
            best_conf = 0.0
            for h in HANDLERS:
                try:
                    out = h(html, url, page)
                    if out:
                        answer = out["answer"]
                        best_conf = out.get("confidence", 0.7)
                        break
                except:
                    pass

            if answer is None:
                q["solved"] = True
                continue

            payload = {
                "email": email,
                "secret": secret,
                "url": url,
                "answer": answer
            }

            try:
                status, resp = post_answer(submit_url, payload)
            except Exception as e:
                q["solved"] = True
                continue

            results.append({"question": url, "sent": payload, "response": resp})

            # If correct, mark solved
            if isinstance(resp, dict) and resp.get("correct") is True:
                q["solved"] = True

            # If next url present → new question window
            if isinstance(resp, dict) and resp.get("url"):
                manager.add(resp["url"])

        browser.close()

    return {"results": results}


# ======================================================
# FLASK ENDPOINT
# ======================================================

@app.route("/solve", methods=["POST"])
def solve_api():
    if not request.is_json:
        return jsonify({"error": "invalid_json"}), 400

    data = request.get_json()

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")

    if not email or not secret or not url:
        return jsonify({"error": "missing_fields"}), 400

    if secret != EXPECTED_SECRET:
        return jsonify({"error": "invalid_secret"}), 403

    try:
        result = solve_quiz_url(url, email, secret)
        return jsonify({"ok": True, "result": result}), 200
    except Exception as e:
        return jsonify({"error": "server_error", "reason": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
