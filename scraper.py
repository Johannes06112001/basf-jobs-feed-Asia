import asyncio
import html
import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime

import aiohttp
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

# The initial BASF page is only used to capture the Azure Search API key.
# The actual data request below fetches all jobs and filters them locally for Asia.
SEARCH_URL = "https://basf.jobs/?currentPage=1&pageSize=1000&addresses%2Fcountry=India"
AZURE_URL = "https://searchui.search.windows.net/indexes/basf-prod/docs/search?api-version=2020-06-30"
BASE_URL = "https://johannes06112001.github.io/basf-jobs-feed-Asia"

ASIA_COUNTRIES = {
    "Afghanistan",
    "Armenia",
    "Azerbaijan",
    "Bahrain",
    "Bangladesh",
    "Bhutan",
    "Brunei",
    "Cambodia",
    "China",
    "Georgia",
    "Hong Kong",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Israel",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Lebanon",
    "Macau",
    "Malaysia",
    "Maldives",
    "Mongolia",
    "Myanmar",
    "Nepal",
    "Oman",
    "Pakistan",
    "Philippines",
    "Qatar",
    "Saudi Arabia",
    "Singapore",
    "South Korea",
    "Sri Lanka",
    "Taiwan",
    "Tajikistan",
    "Thailand",
    "Turkey",
    "Turkmenistan",
    "United Arab Emirates",
    "Uzbekistan",
    "Vietnam",
}

COUNTRY_ALIASES = {
    "Hong Kong SAR": "Hong Kong",
    "Hong Kong S.A.R.": "Hong Kong",
    "Macao": "Macau",
    "Macau SAR": "Macau",
    "Korea": "South Korea",
    "Korea, Republic of": "South Korea",
    "Republic of Korea": "South Korea",
    "UAE": "United Arab Emirates",
    "Viet Nam": "Vietnam",
    "Türkiye": "Turkey",
}

PREFERRED_LOCALES = ["en_US", "en_IN", "en_SG", "en_MY", "en_CN", "en_JP", "de_DE", "de_AT", "de_CH"]
PAGE_SIZE = 1000


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def slugify(text):
    text = (text or "unknown").lower().strip()
    text = re.sub(r"[äÄ]", "ae", text)
    text = re.sub(r"[öÖ]", "oe", text)
    text = re.sub(r"[üÜ]", "ue", text)
    text = re.sub(r"[ß]", "ss", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unknown"


def safe(text):
    return html.escape(str(text or ""), quote=True)


def normalize_country(country):
    country = (country or "").strip()
    return COUNTRY_ALIASES.get(country, country)


def is_asia_job(job):
    addresses = job.get("addresses", [])
    if not isinstance(addresses, list):
        return False
    for addr in addresses:
        if not isinstance(addr, dict):
            continue
        country = normalize_country(addr.get("country"))
        if country in ASIA_COUNTRIES:
            return True
    return False


def first_asia_address(job):
    addresses = job.get("addresses", [])
    if isinstance(addresses, list):
        for addr in addresses:
            if isinstance(addr, dict):
                country = normalize_country(addr.get("country"))
                if country in ASIA_COUNTRIES:
                    return addr, country
    return {}, "Unknown"


def locale_rank(job):
    language = job.get("language", "")
    return PREFERRED_LOCALES.index(language) if language in PREFERRED_LOCALES else 999


def build_job_card(j):
    recruiter_str = ""
    if j.get("recruiter"):
        r = j["recruiter"]
        recruiter_str = " | ".join(
            value for value in [r.get("name", ""), r.get("email", ""), r.get("phone", "")] if value
        )

    return f"""<div class="job" data-job-id="{safe(j.get('job_id'))}" data-country="{safe(j.get('country'))}">
  <h2><a href="{safe(j.get('url'))}">{safe(j.get('title'))}</a></h2>
  <p><strong>Country:</strong> {safe(j.get('country'))}</p>
  <p><strong>Location:</strong> {safe(j.get('city'))}, {safe(j.get('state'))}</p>
  <p><strong>Link:</strong> {safe(j.get('url'))}</p>
  <p><strong>Company:</strong> {safe(j.get('company'))}</p>
  <p><strong>Field:</strong> {safe(j.get('job_field'))}</p>
  <p><strong>Department:</strong> {safe(j.get('department'))}</p>
  <p><strong>Level:</strong> {safe(j.get('job_level'))}</p>
  <p><strong>Type:</strong> {safe(j.get('job_type'))}</p>
  <p><strong>Hybrid:</strong> {'Yes' if j.get('hybrid') else 'No'}</p>
  <p><strong>Posted:</strong> {safe(j.get('date_posted', '')[:10])}</p>
  <p><strong>Description:</strong> {safe(j.get('description'))}</p>
  {f'<p><strong>Contact:</strong> {safe(recruiter_str)}</p>' if recruiter_str else ''}
</div>
"""


async def capture_api_key():
    api_key = None
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_request(request):
            nonlocal api_key
            if "searchui.search.windows.net" in request.url:
                headers = dict(request.headers)
                found_key = headers.get("api-key") or headers.get("Api-Key") or headers.get("authorization") or ""
                if found_key:
                    api_key = found_key

        context.on("request", handle_request)
        try:
            await page.goto(SEARCH_URL, timeout=30000, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            print("⚠️ BASF jobs page timed out while loading. Continuing with captured network requests.")
        await page.wait_for_timeout(10000)
        await browser.close()
    return api_key


async def fetch_raw_jobs(api_key):
    all_raw_jobs = []
    skip = 0

    async with aiohttp.ClientSession() as session:
        while True:
            search_body = {
                "search": "*",
                "select": "*",
                "top": PAGE_SIZE,
                "skip": skip,
                "count": True,
            }
            async with session.post(
                AZURE_URL,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=search_body,
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    print(f"❌ Fehler bei skip={skip}: {err[:300]}")
                    break
                data = await resp.json()

            batch = data.get("value", [])
            total_count = data.get("@odata.count", "?")
            if skip == 0:
                print(f"API meldet @odata.count: {total_count}")

            all_raw_jobs.extend(batch)
            print(f"  skip={skip}: {len(batch)} geladen (gesamt: {len(all_raw_jobs)})")

            if len(batch) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

    return all_raw_jobs


def deduplicate_jobs(raw_jobs):
    job_map = {}
    for job in raw_jobs:
        if not is_asia_job(job):
            continue
        full_id = str(job.get("jobId", ""))
        numeric_id = full_id.split("-")[0] if "-" in full_id else full_id
        if not numeric_id:
            continue
        if numeric_id not in job_map or locale_rank(job) < locale_rank(job_map[numeric_id]):
            job_map[numeric_id] = job
    return job_map


def transform_jobs(job_map):
    jobs = []
    for numeric_id, job in job_map.items():
        addr, country = first_asia_address(job)

        recruiter_raw = job.get("recruiter") or {}
        recruiter = {}
        if recruiter_raw:
            recruiter = {
                "name": f"{recruiter_raw.get('firstName', '')} {recruiter_raw.get('lastName', '')}".strip(),
                "email": recruiter_raw.get("email", ""),
                "phone": recruiter_raw.get("phone", ""),
            }
            recruiter = {k: v for k, v in recruiter.items() if v}

        city = addr.get("city") or addr.get("locationCity") or "Unknown"
        state = addr.get("state") or "Unknown"

        entry = {
            "job_id": numeric_id,
            "title": (job.get("title") or "").strip(),
            "url": job.get("link") or f"https://basf.jobs/job/{numeric_id}/",
            "city": city,
            "state": state,
            "country": country,
            "company": job.get("legalEntity") or "BASF",
            "business_unit": job.get("businessUnit") or "",
            "department": job.get("department") or "",
            "job_field": job.get("jobField") or job.get("category") or "",
            "job_level": job.get("jobLevel") or job.get("customfield1") or "",
            "job_type": job.get("jobType") or job.get("customfield5") or "",
            "hybrid": job.get("hybrid") or False,
            "date_posted": job.get("datePosted") or "",
            "description": strip_html(job.get("description") or "")[:700],
            "recruiter": recruiter if recruiter else None,
        }
        entry = {k: v for k, v in entry.items() if v is not None and v != "" and v != {}}
        jobs.append(entry)

    jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)
    return jobs


def prepare_output_dirs():
    for directory in ["countries", "regions"]:
        if os.path.isdir(directory):
            shutil.rmtree(directory)
        os.makedirs(directory, exist_ok=True)


def generate_region_pages(grouped_by_region, region_slugs):
    for (country, state, city), region_jobs in sorted(
        grouped_by_region.items(), key=lambda item: (item[0][0].lower(), item[0][1].lower(), item[0][2].lower())
    ):
        slug = region_slugs[(country, state, city)]
        rows = "".join(build_job_card(j) for j in region_jobs)
        country_slug = slugify(country)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs – {safe(city)}, {safe(country)}</title></head>
<body>
<p><a href="{BASE_URL}/countries/{country_slug}.html">← Back to {safe(country)}</a> | <a href="{BASE_URL}/index_lite.html">Asia overview</a></p>
<h1>BASF Jobs – {safe(city)}, {safe(state)}, {safe(country)}</h1>
<p>{len(region_jobs)} position(s)</p>
{rows}
</body>
</html>"""

        with open(f"regions/{slug}.html", "w", encoding="utf-8") as f:
            f.write(html_content)


def generate_country_pages(grouped_by_country, grouped_by_region, region_slugs):
    for country, country_jobs in sorted(grouped_by_country.items(), key=lambda item: item[0].lower()):
        country_slug = slugify(country)
        country_regions = sorted(
            [key for key in grouped_by_region if key[0] == country],
            key=lambda key: (key[1].lower(), key[2].lower()),
        )

        rows = ""
        current_state = None
        for _, state, city in country_regions:
            if state != current_state:
                if current_state is not None:
                    rows += "</ul>\n"
                rows += f"<h2>{safe(state)}</h2>\n<ul>\n"
                current_state = state

            slug = region_slugs[(country, state, city)]
            region_jobs = grouped_by_region[(country, state, city)]
            region_url = f"{BASE_URL}/regions/{slug}.html"
            rows += f'<li><a href="{region_url}">{safe(city)}</a> ({len(region_jobs)} position(s))<ul>\n'
            for j in region_jobs:
                job_field = j.get("job_field", "")
                field_tag = f"[{safe(job_field)}] " if job_field else ""
                job_level = j.get("job_level", "")
                level_tag = f"[{safe(job_level)}] " if job_level else ""
                rows += (
                    f'  <li>{safe(j.get("date_posted", "")[:10])} – '
                    f'{field_tag}{level_tag}'
                    f'<a href="{safe(j.get("url", ""))}">{safe(j.get("title", ""))}</a></li>\n'
                )
            rows += "</ul></li>\n"

        if current_state is not None:
            rows += "</ul>\n"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs {safe(country)} – Overview</title></head>
<body>
<p><a href="{BASE_URL}/index_lite.html">← Asia overview</a></p>
<h1>BASF Job Openings {safe(country)}</h1>
<p>Total: {len(country_jobs)} positions | {len(country_regions)} locations</p>
{rows}
</body>
</html>"""

        with open(f"countries/{country_slug}.html", "w", encoding="utf-8") as f:
            f.write(html_content)


def generate_index_pages(jobs, grouped_by_country, grouped_by_region):
    index_rows = "<h2>Countries</h2>\n<ul>\n"
    lite_rows = "<h2>Countries</h2>\n<ul>\n"

    for country, country_jobs in sorted(grouped_by_country.items(), key=lambda item: item[0].lower()):
        country_slug = slugify(country)
        country_url = f"{BASE_URL}/countries/{country_slug}.html"
        location_count = len([key for key in grouped_by_region if key[0] == country])
        index_rows += f'<li><a href="{country_url}">{safe(country)}</a> ({len(country_jobs)} position(s), {location_count} location(s))<ul>\n'
        lite_rows += f'<li><a href="{country_url}">{safe(country)}</a> ({len(country_jobs)} positions, {location_count} locations)</li>\n'

        for j in country_jobs[:100]:
            job_field = j.get("job_field", "")
            field_tag = f"[{safe(job_field)}] " if job_field else ""
            job_level = j.get("job_level", "")
            level_tag = f"[{safe(job_level)}] " if job_level else ""
            index_rows += (
                f'  <li>{safe(j.get("date_posted", "")[:10])} – '
                f'{safe(j.get("city", ""))} – {field_tag}{level_tag}'
                f'<a href="{safe(j.get("url", ""))}">{safe(j.get("title", ""))}</a></li>\n'
            )

        if len(country_jobs) > 100:
            index_rows += f'  <li>More positions available on the dedicated <a href="{country_url}">{safe(country)} page</a>.</li>\n'
        index_rows += "</ul></li>\n"

    index_rows += "</ul>\n"
    lite_rows += "</ul>\n"

    country_count = len(grouped_by_country)
    location_count = len(grouped_by_region)

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs Asia – Overview</title></head>
<body>
<h1>BASF Job Openings Asia</h1>
<p>Total: {len(jobs)} positions | {country_count} countries | {location_count} locations</p>
<p>This page is optimized for LLM discovery. Each Asian country has its own readable country page in <code>/countries/</code>; detailed city/location pages are available in <code>/regions/</code>.</p>
{index_rows}
</body>
</html>"""

    lite_index_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs Asia – Country Overview</title></head>
<body>
<h1>BASF Job Openings Asia</h1>
<p>Total: {len(jobs)} positions | {country_count} countries | {location_count} locations</p>
<p>Country pages use the same structure as the India page and are intentionally simple for LLM parsing.</p>
{lite_rows}
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    with open("index_lite.html", "w", encoding="utf-8") as f:
        f.write(lite_index_html)


def write_jobs_json(jobs):
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    output = {
        "last_updated": timestamp,
        "scope": "Asia",
        "total_active": len(jobs),
        "countries": sorted({j.get("country", "Unknown") for j in jobs}),
        "jobs": jobs,
    }
    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


async def scrape_jobs():
    api_key = await capture_api_key()
    if not api_key:
        print("❌ Kein API Key gefunden!")
        return

    print("✅ API Key gefunden")
    raw_jobs = await fetch_raw_jobs(api_key)
    print(f"Rohdaten: {len(raw_jobs)} Jobs aus allen Ländern und Locales")

    job_map = deduplicate_jobs(raw_jobs)
    print(f"Nach Asia-Filter und Deduplizierung: {len(job_map)} unique Jobs")

    jobs = transform_jobs(job_map)
    prepare_output_dirs()
    write_jobs_json(jobs)
    print(f"✅ jobs.json gespeichert — {len(jobs)} Asia Jobs!")

    grouped_by_country = defaultdict(list)
    grouped_by_region = defaultdict(list)
    for job in jobs:
        country = job.get("country", "Unknown")
        state = job.get("state", "Unknown")
        city = job.get("city", "Unknown")
        grouped_by_country[country].append(job)
        grouped_by_region[(country, state, city)].append(job)

    region_slugs = {
        key: f"region-{slugify(key[0])}-{slugify(key[1])}-{slugify(key[2])}"
        for key in grouped_by_region
    }

    generate_region_pages(grouped_by_region, region_slugs)
    print(f"✅ {len(grouped_by_region)} region pages generated!")

    generate_country_pages(grouped_by_country, grouped_by_region, region_slugs)
    print(f"✅ {len(grouped_by_country)} country pages generated!")

    generate_index_pages(jobs, grouped_by_country, grouped_by_region)
    print("✅ index.html und index_lite.html saved!")


asyncio.run(scrape_jobs())