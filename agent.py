import os
import asyncio
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
        page = await browser.new_page()
        
        # Format SEEK search URL dynamically based on filters
        search_url = f"https://www.seek.com.au/{SEARCH_KEYWORD}-jobs/in-{LOCATION}?sortmode=KeywordRelevance"
        print(f"Navigating to SEEK: {search_url}")
        await page.goto(search_url)
        await page.wait_for_timeout(3000) 

        # Grab job card elements from the page
        cards = await page.locator('article[data-card-type="JobCard"]').all()
        
        for card in cards[:10]: # Evaluate the top 10 freshest listings
            try:
                title_element = card.locator('a[data-automation="jobTitle"]')
                title = await title_element.inner_text()
                link = "https://www.seek.com.au" + (await title_element.get_attribute("href")).split("?")[0]
                company = await card.locator('a[data-automation="jobCompany"]').inner_text()
            except Exception:
                continue
                
            jobs.append({"title": title, "company": company, "link": link})
            
        # Deep dive into individual job listings to extract job descriptions
        for job in jobs:
            print(f"Scraping description for: {job['title']} at {job['company']}")
            await page.goto(job["link"])
            await page.wait_for_timeout(2000)
            try:
                desc_element = await page.locator('div[data-automation="jobAdDetails"]').inner_html()
                job["description"] = BeautifulSoup(desc_element, "html.parser").get_text(separator=" ")
            except Exception:
                job["description"] = ""
                
        await browser.close()
    return jobs

def evaluate_job_with_ai(job_desc):
    """Uses LLM to evaluate if the JD matches a Business Analyst or Senior BA profile."""
    prompt = f"""
    Analyze the following job description. Determine if this role explicitly matches a Business Analyst or Senior Business Analyst position, and identify if it contains any of these attributes or related skills: {', '.join(REQUIRED_SKILLS)}.
    
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
            model='gemini-2.5-flash',
            contents=prompt,
        )
        text = response.text.strip().replace("```json", "").replace("
```", "")
        import json
        return json.loads(text)
    except Exception as e:
        print(f"AI evaluation failed for this role: {e}")
        return {"matches": False, "matched_skills": []}

def send_email(matched_jobs):
    if not matched_jobs:
        print("No matching Business Analyst jobs found today.")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Daily SEEK BA Agent Report: {len(matched_jobs)} Roles Identified"
    msg['From'] = os.environ["EMAIL_SENDER"]
    msg['To'] = os.environ["EMAIL_RECEIVER"]

    # Build HTML Email Body
    html = f"<h2>Your Daily AI Curated BA Job Digest ({LOCATION})</h2><hr/>"
    for job in matched_jobs:
        html += f"""
        <div style="margin-bottom: 25px; padding: 15px; border-left: 4px solid #0056b3; background-color: #f8f9fa;">
            <h3 style="margin-top: 0;"><a href="{job['link']}" style="color: #0056b3; text-decoration: none;">{job['title']}</a></h3>
            <p style="margin: 5px 0;"><strong>Company:</strong> {job['company']}</p>
            <p style="margin: 5px 0;"><strong>Identified Criteria/Skills:</strong> {', '.join(job['skills'])}</p>
        </div>
        """
    
    msg.attach(MIMEText(html, 'html'))

    # Send securely to self via Gmail SMTP
    print("Connecting to Gmail SMTP server...")
    with smtplib.SMTP(os.environ["SMTP_SERVER"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(msg['From'], msg['To'], msg.as_string())
    print("Job digest successfully delivered to your Gmail inbox.")

async def main():
    print("Starting AI Job Agent...")
    raw_jobs = await search_seek()
    
    matched_jobs = []
    print(f"Processing {len(raw_jobs)} fetched listings through Gemini API...")
    for job in raw_jobs:
        if not job["description"]:
            continue
        ai_result = evaluate_job_with_ai(job["description"])
        if ai_result.get("matches"):
            job["skills"] = ai_result.get("matched_skills", [])
            matched_jobs.append(job)
            
    send_email(matched_jobs)

if __name__ == "__main__":
    asyncio.run(main())