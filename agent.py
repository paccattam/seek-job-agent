import os
import asyncio
import json
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from google import genai
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Filter Configuration ---
SEARCH_KEYWORD = "Business Analyst"
LOCATION = "Melbourne"

# The core criteria and skills the AI agent will look for within the JD
REQUIRED_SKILLS = [
    "Business Analyst",
    "Senior Business Analyst",
    "Agile",
    "Requirements Gathering",
    "Process Mapping"
]

# Initialize Gemini Client
ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def search_seek():
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Use a realistic user-agent to avoid bot detection / Cloudflare blocks
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # FIX 1: URL-encode the keyword (spaces → hyphens) so SEEK returns results
        keyword_slug = SEARCH_KEYWORD.replace(" ", "-")
        search_url = f"https://www.seek.com.au/{keyword_slug}-jobs/in-{LOCATION}?sortmode=KeywordRelevance"
        print(f"[SCRAPER] Navigating to SEEK: {search_url}")

        await page.goto(search_url)

        # FIX 2: Wait for network to settle instead of a fixed sleep
        await page.wait_for_load_state("networkidle")

        # FIX 3: Wait for at least one article element before reading the DOM
        try:
            await page.wait_for_selector("article", timeout=10000)
        except Exception:
            print("[SCRAPER] Timed out waiting for job cards — saving debug screenshot.")
            await page.screenshot(path="seek_debug.png")
            print(f"[DEBUG] Page title: {await page.title()}")
            print(f"[DEBUG] Final URL: {page.url}")
            await browser.close()
            return jobs

        # --- Debug info so you can see what was loaded ---
        print(f"[DEBUG] Page title: {await page.title()}")
        print(f"[DEBUG] Final URL: {page.url}")

        # FIX 4: Try primary selector, then fall back to alternatives if SEEK changed their DOM
        cards = await page.locator('article[data-card-type="JobCard"]').all()
        if not cards:
            print("[SCRAPER] Primary selector matched nothing — trying fallbacks...")
            cards = await page.locator('[data-testid="job-card"]').all()
        if not cards:
            cards = await page.locator("article").all()

        print(f"[SCRAPER] Found {len(cards)} job card(s) on the page.")

        for card in cards[:10]:  # Top 10 freshest listings
            try:
                title_element = card.locator('a[data-automation="jobTitle"]')
                title = await title_element.inner_text()
                href = await title_element.get_attribute("href")
                link = "https://www.seek.com.au" + href.split("?")[0]
                company = await card.locator('a[data-automation="jobCompany"]').inner_text()
            except Exception:
                continue

            jobs.append({"title": title, "company": company, "link": link})

        # Deep-dive into each listing to grab the full job description
        for job in jobs:
            print(f"[SCRAPER] Fetching description: {job['title']} at {job['company']}")
            await page.goto(job["link"])
            await page.wait_for_load_state("networkidle")

            # FIX 5: Fallback selectors for the job description panel
            desc_html = ""
            for selector in [
                'div[data-automation="jobAdDetails"]',
                '[data-automation="jobDescription"]',
                '[data-testid="job-detail-page"]',
            ]:
                try:
                    desc_html = await page.locator(selector).inner_html(timeout=5000)
                    if desc_html:
                        break
                except Exception:
                    continue

            job["description"] = (
                BeautifulSoup(desc_html, "html.parser").get_text(separator=" ")
                if desc_html
                else ""
            )

        await browser.close()
    return jobs


def evaluate_job_with_ai(job_desc):
    """Uses Gemini to evaluate whether the JD matches a BA / Senior BA profile."""
    prompt = f"""
Analyze the following job description. Determine if this role explicitly matches a Business Analyst or Senior Business Analyst position, and identify if it contains any of these attributes or skills: {', '.join(REQUIRED_SKILLS)}.

Job Description:
{job_desc}

Respond in strict JSON format. Do not include markdown code block wrappers:
{{
    "matches": true/false,
    "matched_skills": ["Senior Business Analyst", "Agile", "Requirements Gathering"]
}}
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(text)
    except Exception as e:
        print(f"[AI AGENT] Evaluation failed: {e}")
        return {"matches": False, "matched_skills": []}


def send_email(matched_jobs):
    msg = MIMEMultipart("alternative")
    msg["From"] = os.environ["EMAIL_SENDER"]
    msg["To"] = os.environ["EMAIL_RECEIVER"]

    # Support comma-separated recipients stored in the secret
    recipient_list = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]

    if matched_jobs:
        print(f"[EMAIL] Building digest for {len(matched_jobs)} matching role(s)...")
        msg["Subject"] = f"Daily SEEK BA Agent Report: {len(matched_jobs)} Role(s) Identified"

        html = f"<h2>Your Daily AI-Curated BA Job Digest ({LOCATION})</h2><hr/>"
        for job in matched_jobs:
            html += f"""
            <div style="margin-bottom:25px;padding:15px;border-left:4px solid #0056b3;background:#f8f9fa;">
                <h3 style="margin-top:0;">
                    <a href="{job['link']}" style="color:#0056b3;text-decoration:none;">{job['title']}</a>
                </h3>
                <p style="margin:5px 0;"><strong>Company:</strong> {job['company']}</p>
                <p style="margin:5px 0;"><strong>Matched Skills:</strong> {', '.join(job['skills'])}</p>
            </div>
            """
    else:
        print("[EMAIL] No matches today — sending status notification...")
        msg["Subject"] = "Daily SEEK BA Agent Report: 0 Jobs Found Today"
        html = f"""
        <h2>Your Daily AI-Curated BA Job Digest ({LOCATION})</h2><hr/>
        <p>The automated scan completed successfully, but <strong>no new jobs</strong> matched your criteria today.</p>
        <p style="color:#666;font-size:12px;">This email confirms your agent is live and functional.</p>
        """

    msg.attach(MIMEText(html, "html"))

    print("[EMAIL] Connecting to Gmail SMTP...")
    try:
        with smtplib.SMTP(os.environ["SMTP_SERVER"], int(os.environ["SMTP_PORT"])) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.sendmail(msg["From"], recipient_list, msg.as_string())
        print(f"[EMAIL] Delivered to: {msg['To']}")
    except Exception as e:
        print(f"[EMAIL] SMTP delivery failed: {e}")


async def main():
    print("--- STARTING SEEK AI JOB AGENT ---")
    raw_jobs = await search_seek()
    print(f"[CORE] Scraping complete. Raw jobs found: {len(raw_jobs)}")

    matched_jobs = []
    print("[CORE] Running listings through Gemini evaluation...")
    for job in raw_jobs:
        if not job.get("description"):
            print(f"[CORE] Skipping '{job['title']}' — no description found.")
            continue

        result = evaluate_job_with_ai(job["description"])
        print(f"[AI AGENT] '{job['title']}' → matches: {result.get('matches')}")

        if result.get("matches"):
            job["skills"] = result.get("matched_skills", [])
            matched_jobs.append(job)

    print(f"[CORE] Done: {len(matched_jobs)}/{len(raw_jobs)} roles passed filters.")
    send_email(matched_jobs)


if __name__ == "__main__":
    asyncio.run(main())
