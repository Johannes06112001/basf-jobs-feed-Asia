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
# The actual data request below fetches all jobs and filters them locally for Asia/APAC.
SEARCH_URL = "https://basf.jobs/?currentPage=1&pageSize=1000&addresses%2Fcountry=India"
AZURE_URL = "https://searchui.search.windows.net/indexes/basf-prod/docs/search?api-version=2020-06-30"

# Public GitHub Pages base URL used inside generated JSON.
# Change this single value when the repository is moved to another account.
BASE_URL = "https://johannes06112001.github.io/basf-jobs-feed-Asia"

DESCRIPTION_PREVIEW_CHARS = 320
DESCRIPTION_DETAIL_CHARS = 2500
PAGE_SIZE = 1000

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

INVALID_URL_TOKENS = [
    "%E2%80%94",
    "—",
    "undefined",
    "null",
    "[NUMBER]",
    "XXXXXX",
    "REQ_",
]


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{cut}..." if cut else text[:max_chars]


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


def country_sort_key(country):
    # Keep India first because the assistant is India-primary, then alphabetical.
    return (0 if country == "India" else 1, country.lower())


def is_valid_basf_url(url):
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith("https://basf.jobs/"):
        return False
    lower_url = url.lower()
    if any(token.lower() in lower_url for token in INVALID_URL_TOKENS):
        return False
    return True


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


def job_detail_path(job):
    return f"data/jobs/{slugify(job.get('job_id'))}.json"


def compact_job(j, include_preview=True, include_detail_path=True):
    entry = {
        "job_id": j.get("job_id", ""),
        "title": j.get("title", ""),
        "url": j.get("url", ""),
        "country": j.get("country", ""),
        "city": j.get("city", ""),
        "state": j.get("state", ""),
        "job_field": j.get("job_field", ""),
        "job_level": j.get("job_level", ""),
        "job_type": j.get("job_type", ""),
        "date_posted": j.get("date_posted", ""),
    }
    if include_preview:
        preview = shorten(j.get("description", ""), DESCRIPTION_PREVIEW_CHARS)
        if preview:
            entry["description_preview"] = preview
    if include_detail_path:
        entry["detail_path"] = job_detail_path(j)
    return {k: v for k, v in entry.items() if v not in ("", None, {})}


def detail_job(j):
    entry = {
        "job_id": j.get("job_id", ""),
        "title": j.get("title", ""),
        "url": j.get("url", ""),
        "country": j.get("country", ""),
        "city": j.get("city", ""),
        "state": j.get("state", ""),
        "job_field": j.get("job_field", ""),
        "job_level": j.get("job_level", ""),
        "job_type": j.get("job_type", ""),
        "company": j.get("company", ""),
        "department": j.get("department", ""),
        "business_unit": j.get("business_unit", ""),
        "hybrid": j.get("hybrid", False),
        "date_posted": j.get("date_posted", ""),
        "description": shorten(j.get("description", ""), DESCRIPTION_DETAIL_CHARS),
    }
    return {k: v for k, v in entry.items() if v not in ("", None, {})}


def search_index_job(j):
    text_parts = [
        j.get("title", ""),
        j.get("country", ""),
        j.get("city", ""),
        j.get("state", ""),
        j.get("job_field", ""),
        j.get("job_level", ""),
        j.get("job_type", ""),
    ]
    return {
        "job_id": j.get("job_id", ""),
        "title": j.get("title", ""),
        "url": j.get("url", ""),
        "country": j.get("country", ""),
        "city": j.get("city", ""),
        "job_field": j.get("job_field", ""),
        "job_level": j.get("job_level", ""),
        "date_posted": j.get("date_posted", ""),
        "detail_path": job_detail_path(j),
        "search_text": " | ".join(part for part in text_parts if part),
    }


def build_country_job_line(j):
    job_field = j.get("job_field", "")
    field_tag = f"[{safe(job_field)}] " if job_field else ""
    job_level = j.get("job_level", "")
    level_tag = f"[{safe(job_level)}] " if job_level else ""
    job_type = j.get("job_type", "")
    type_tag = f"[{safe(job_type)}] " if job_type else ""
    posted = safe(j.get("date_posted", "")[:10])
    city = safe(j.get("city", ""))
    state = safe(j.get("state", ""))

    return (
        f'<li data-job-id="{safe(j.get("job_id"))}" data-field="{safe(job_field)}" '
        f'data-city="{city}" data-state="{state}">'
        f'{posted} – {field_tag}{level_tag}{type_tag}'
        f'<a href="{safe(j.get("url"))}">{safe(j.get("title"))}</a>'
        f' — {city}, {state} '
        f'(<a href="../{safe(job_detail_path(j))}">detail JSON</a>)</li>\n'
    )


async def capture_api_key():
    api_key = None
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            )
        )
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
    skipped_without_valid_url = 0

    for numeric_id, job in job_map.items():
        url = (job.get("link") or "").strip()
        if not is_valid_basf_url(url):
            skipped_without_valid_url += 1
            continue

        addr, country = first_asia_address(job)

        city = addr.get("city") or addr.get("locationCity") or "Unknown"
        state = addr.get("state") or "Unknown"
        description = strip_html(job.get("description") or "")

        entry = {
            "job_id": numeric_id,
            "title": (job.get("title") or "").strip(),
            "url": url,
            "city": city,
            "state": state,
            "country": country,
            "company": job.get("legalEntity") or "BASF",
            "business_unit": job.get("businessUnit") or "",
            "department": job.get("department") or "",
            "job_field": job.get("jobField") or job.get("category") or "Other",
            "job_level": job.get("jobLevel") or job.get("customfield1") or "",
            "job_type": job.get("jobType") or job.get("customfield5") or "",
            "hybrid": job.get("hybrid") or False,
            "date_posted": job.get("datePosted") or "",
            "description": description,
        }
        entry = {k: v for k, v in entry.items() if v is not None and v != "" and v != {}}
        jobs.append(entry)

    if skipped_without_valid_url:
        print(f"⚠️ {skipped_without_valid_url} jobs skipped because no verified basf.jobs URL was present.")

    jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)
    return jobs


def prepare_output_dirs():
    # Remove old generated folders, including the now deprecated region pages.
    for directory in ["countries", "regions", "data"]:
        if os.path.isdir(directory):
            shutil.rmtree(directory)

    os.makedirs("countries", exist_ok=True)
    os.makedirs("data/countries", exist_ok=True)
    os.makedirs("data/jobs", exist_ok=True)


def field_counts(jobs):
    counts = defaultdict(int)
    for job in jobs:
        counts[job.get("job_field", "Other")] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())))


def write_detail_files(jobs, timestamp):
    for job in jobs:
        payload = {
            "last_updated": timestamp,
            "scope": "job_detail",
            "llm_instruction": "Use this file only after a candidate job was selected. Copy the BASF URL exactly; never construct job links.",
            "job": detail_job(job),
        }
        path = job_detail_path(job)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def write_json_files(jobs, grouped_by_country, grouped_by_country_field):
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    write_detail_files(jobs, timestamp)

    country_routes = []
    for country in sorted(grouped_by_country.keys(), key=country_sort_key):
        country_slug = slugify(country)
        country_jobs = grouped_by_country[country]
        fields = field_counts(country_jobs)

        field_routes = []
        for field in fields:
            field_slug = slugify(field)
            field_jobs = grouped_by_country_field[(country, field)]
            field_dir = f"data/countries/{country_slug}/fields"
            os.makedirs(field_dir, exist_ok=True)

            field_path = f"{field_dir}/{field_slug}.json"
            field_payload = {
                "last_updated": timestamp,
                "scope": "country_field",
                "country": country,
                "job_field": field,
                "total_active": len(field_jobs),
                "llm_instruction": (
                    "Use this small file for country-and-function-specific matching. "
                    "Descriptions are short previews. Fetch detail_path only for shortlisted roles. "
                    "Copy job URLs exactly. Never construct basf.jobs links."
                ),
                "jobs": [compact_job(job, include_preview=True, include_detail_path=True) for job in field_jobs],
            }
            with open(field_path, "w", encoding="utf-8") as f:
                json.dump(field_payload, f, ensure_ascii=False, indent=2)

            field_routes.append(
                {
                    "job_field": field,
                    "count": len(field_jobs),
                    "json_path": field_path,
                    "json_url": f"{BASE_URL}/{field_path}",
                }
            )

        country_json_path = f"data/countries/{country_slug}.json"
        country_payload = {
            "last_updated": timestamp,
            "scope": "country",
            "country": country,
            "total_active": len(country_jobs),
            "fields": fields,
            "llm_instruction": (
                f"This file contains only BASF jobs in {country}. "
                "Descriptions are short previews. Fetch detail_path only for shortlisted roles. "
                "For role-specific searches, use the field JSON files when available. "
                "Copy job URLs exactly and never generate basf.jobs links."
            ),
            "jobs": [compact_job(job, include_preview=True, include_detail_path=True) for job in country_jobs],
        }
        with open(country_json_path, "w", encoding="utf-8") as f:
            json.dump(country_payload, f, ensure_ascii=False, indent=2)

        country_routes.append(
            {
                "country": country,
                "count": len(country_jobs),
                "html_path": f"countries/{country_slug}.html",
                "html_url": f"{BASE_URL}/countries/{country_slug}.html",
                "json_path": country_json_path,
                "json_url": f"{BASE_URL}/{country_json_path}",
                "fields": field_routes,
            }
        )

    routing_payload = {
        "last_updated": timestamp,
        "scope": "Asia/APAC",
        "total_active": len(jobs),
        "llm_instruction": (
            "Start here only to route the search. Do not load the full Asia dataset first. "
            "Pick the requested country, then fetch that country's JSON. "
            "If the user asks for a function, prefer the matching country/function JSON. "
            "Default to India when no country is specified. Use wider Asia/APAC only as fallback. "
            "Use json_path for portable same-repository navigation; use json_url when an absolute URL is required. "
            "Copy BASF job URLs exactly from source."
        ),
        "countries": country_routes,
    }
    with open("data/llm-routing.json", "w", encoding="utf-8") as f:
        json.dump(routing_payload, f, ensure_ascii=False, indent=2)

    with open("data/search-index.jsonl", "w", encoding="utf-8") as f:
        for job in jobs:
            f.write(json.dumps(search_index_job(job), ensure_ascii=False) + "\n")

    # Backward-compatible aggregate file. Agents should prefer data/llm-routing.json.
    aggregate_payload = {
        "last_updated": timestamp,
        "scope": "Asia/APAC",
        "total_active": len(jobs),
        "llm_instruction": (
            "Large aggregate file. Prefer data/llm-routing.json, country JSON files, "
            "field JSON files, and job detail JSON files to keep context small."
        ),
        "countries": sorted(grouped_by_country.keys(), key=country_sort_key),
        "jobs": [compact_job(job, include_preview=False, include_detail_path=True) for job in jobs],
    }
    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(aggregate_payload, f, ensure_ascii=False, indent=2)


def generate_country_pages(grouped_by_country, grouped_by_country_field):
    for country in sorted(grouped_by_country.keys(), key=country_sort_key):
        country_jobs = grouped_by_country[country]
        country_slug = slugify(country)
        fields = field_counts(country_jobs)

        field_nav = "<ul>\n"
        for field, count in fields.items():
            field_slug = slugify(field)
            field_nav += (
                f'<li><a href="#field-{field_slug}">{safe(field)}</a> ({count}) '
                f'| <a href="../data/countries/{country_slug}/fields/{field_slug}.json">JSON</a></li>\n'
            )
        field_nav += "</ul>\n"

        rows = ""
        for field in fields:
            field_slug = slugify(field)
            field_jobs = grouped_by_country_field[(country, field)]
            rows += f'<section id="field-{field_slug}">\n'
            rows += f"<h2>{safe(field)} ({len(field_jobs)})</h2>\n<ul>\n"
            for job in field_jobs:
                rows += build_country_job_line(job)
            rows += "</ul>\n</section>\n"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BASF Jobs {safe(country)} – LLM Country Page</title>
</head>
<body>
  <p><a href="../index_lite.html">← Asia/APAC country index</a></p>
  <h1>BASF Job Openings {safe(country)}</h1>
  <p>Total: {len(country_jobs)} active position(s).</p>

  <h2>LLM usage</h2>
  <p>This page contains ONLY jobs in {safe(country)}. Use it when the user explicitly asks for {safe(country)}, or when {safe(country)} is the India-first/default country.</p>
  <p>For a smaller context window, use the country JSON first:
    <a href="../data/countries/{country_slug}.json">../data/countries/{country_slug}.json</a>
  </p>
  <p>For function-specific searches, use the matching field JSON below. Fetch detail JSON only for shortlisted roles. Copy every BASF job URL exactly from the source. Never construct job links from titles or IDs.</p>

  <h2>Fields in {safe(country)}</h2>
  {field_nav}

  <h2>Jobs by field</h2>
  {rows}
</body>
</html>"""

        with open(f"countries/{country_slug}.html", "w", encoding="utf-8") as f:
            f.write(html_content)


def generate_index_pages(jobs, grouped_by_country):
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    country_count = len(grouped_by_country)

    rows = "<ul>\n"
    for country in sorted(grouped_by_country.keys(), key=country_sort_key):
        country_slug = slugify(country)
        country_jobs = grouped_by_country[country]
        fields = field_counts(country_jobs)
        top_fields = ", ".join(f"{field} ({count})" for field, count in list(fields.items())[:5])

        rows += (
            f'<li><strong>{safe(country)}</strong> — {len(country_jobs)} position(s) '
            f'| <a href="countries/{country_slug}.html">HTML</a> '
            f'| <a href="data/countries/{country_slug}.json">JSON</a>'
        )
        if top_fields:
            rows += f"<br>Top fields: {safe(top_fields)}"
        rows += "</li>\n"
    rows += "</ul>\n"

    shared_body = f"""
<h1>BASF Job Openings Asia/APAC</h1>
<p>Last updated: {safe(timestamp)}</p>
<p>Total: {len(jobs)} positions | {country_count} countries.</p>

<h2>LLM routing instruction</h2>
<p>Do not load the full Asia job list into the context first. Start with the routing JSON, select the requested country, then fetch only that country or country/function file.</p>
<p><strong>Routing JSON:</strong> <a href="data/llm-routing.json">data/llm-routing.json</a></p>
<p><strong>Search index:</strong> <a href="data/search-index.jsonl">data/search-index.jsonl</a></p>
<p>Default behavior for the agent: India first when no country is specified. If the user names a country, use that country first. Wider Asia/APAC is fallback only.</p>
<p>Links on this page are relative on purpose, so the site remains portable when the repository is copied to another GitHub account.</p>

<h2>Country pages</h2>
{rows}
"""

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs Asia/APAC – LLM Routing Index</title></head>
<body>
{shared_body}
</body>
</html>"""

    lite_index_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BASF Jobs Asia/APAC – Country Index</title></head>
<body>
{shared_body}
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    with open("index_lite.html", "w", encoding="utf-8") as f:
        f.write(lite_index_html)


async def scrape_jobs():
    api_key = await capture_api_key()
    if not api_key:
        raise RuntimeError("No BASF Search API key found. The feed was not updated.")

    print("✅ API Key gefunden")
    raw_jobs = await fetch_raw_jobs(api_key)
    print(f"Rohdaten: {len(raw_jobs)} Jobs aus allen Ländern und Locales")

    job_map = deduplicate_jobs(raw_jobs)
    print(f"Nach Asia/APAC-Filter und Deduplizierung: {len(job_map)} unique Jobs")

    jobs = transform_jobs(job_map)
    prepare_output_dirs()

    grouped_by_country = defaultdict(list)
    grouped_by_country_field = defaultdict(list)

    for job in jobs:
        country = job.get("country", "Unknown")
        field = job.get("job_field", "Other")
        grouped_by_country[country].append(job)
        grouped_by_country_field[(country, field)].append(job)

    write_json_files(jobs, grouped_by_country, grouped_by_country_field)
    print("✅ JSON routing, search index, country, field, and detail files generated!")
    print(f"✅ jobs.json gespeichert — {len(jobs)} verified Asia/APAC Jobs!")

    generate_country_pages(grouped_by_country, grouped_by_country_field)
    print(f"✅ {len(grouped_by_country)} country pages generated!")

    generate_index_pages(jobs, grouped_by_country)
    print("✅ index.html und index_lite.html saved!")


asyncio.run(scrape_jobs())
