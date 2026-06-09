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
    "Turkiye": "Turkey",
    "Turky": "Turkey",
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

INTENT_GROUPS = {
    "software-engineering": {
        "display_name": "Software Engineering / Developer / Full Stack",
        "aliases": [
            "software engineer",
            "software developer",
            "developer",
            "full stack",
            "full-stack",
            "frontend",
            "front end",
            "backend",
            "back end",
            "application developer",
            "platform engineer",
            "cloud engineer",
            "data engineer",
            "software",
            "programmer",
            "entwickler",
            "softwareentwickler",
            "software engineer werden",
        ],
        "primary_fields": ["Digitalization", "Information Technology & Services"],
        "adjacent_fields": ["Applications Technology", "Engineering"],
        "title_keywords": ["software", "developer", "full stack", "backend", "frontend", "application", "platform", "cloud", "engineer", "scrum", "product owner"],
    },
    "backend-engineering": {
        "display_name": "Backend Engineering",
        "aliases": ["backend", "back end", "backend engineer", "backend developer", "api developer", "server side", "erstes"],
        "primary_fields": ["Digitalization", "Information Technology & Services"],
        "adjacent_fields": ["Applications Technology", "Engineering"],
        "title_keywords": ["backend", "back end", "developer", "software", "application", "platform", "engineer", "api"],
    },
    "sap-business-applications": {
        "display_name": "SAP / Business Applications",
        "aliases": [
            "sap",
            "business applications",
            "business application",
            "s/4hana",
            "s4 hana",
            "abap",
            "sap consultant",
            "sap developer",
            "order-to-cash",
            "order to cash",
            "order-to-invoice",
            "mm",
            "atp",
        ],
        "primary_fields": ["Digitalization", "Information Technology & Services"],
        "adjacent_fields": ["Applications Technology", "Logistics & Supply Chain Management"],
        "title_keywords": ["sap", "s/4", "s4", "abap", "business application", "consultant", "developer", "order", "mm", "atp"],
    },
    "business-informatics": {
        "display_name": "Business Informatics / Wirtschaftsinformatik",
        "aliases": ["wirtschaftsinformatiker", "wirtschaftsinformatik", "business informatics", "business it", "it project", "product owner", "portfolio", "scrum", "data", "sap"],
        "primary_fields": ["Digitalization", "Information Technology & Services"],
        "adjacent_fields": ["Finance, Controlling & Audit", "Logistics & Supply Chain Management", "Applications Technology"],
        "title_keywords": ["digital", "data", "sap", "software", "scrum", "product", "portfolio", "application", "business", "solution"],
    },
    "digitalization": {
        "display_name": "Digitalization / IT / Data",
        "aliases": ["digitalization", "digitalisation", "digital", "it", "information technology", "data", "automation", "ai", "cyber security", "cybersecurity", "cloud", "digitalisierung"],
        "primary_fields": ["Digitalization", "Information Technology & Services"],
        "adjacent_fields": ["Applications Technology", "Engineering"],
        "title_keywords": ["digital", "data", "automation", "ai", "cyber", "cloud", "software", "application", "solution", "architect"],
    },
    "finance": {
        "display_name": "Finance / Controlling / Audit",
        "aliases": ["finance", "controlling", "audit", "accounting", "tax", "treasury", "reporting", "mis", "controller"],
        "primary_fields": ["Finance, Controlling & Audit", "Finance", "Controlling", "Accounting"],
        "adjacent_fields": ["Administration & Office Support", "Procurement"],
        "title_keywords": ["finance", "controlling", "audit", "account", "tax", "treasury", "reporting", "mis", "controller"],
    },
    "supply-chain-logistics": {
        "display_name": "Supply Chain / Logistics",
        "aliases": ["supply chain", "logistics", "scm", "planning", "warehouse", "inventory", "materials management", "demand planning"],
        "primary_fields": ["Logistics & Supply Chain Management", "Supply Chain", "Logistics"],
        "adjacent_fields": ["Procurement", "Production, Maintenance & Technical Services"],
        "title_keywords": ["supply", "logistics", "planning", "warehouse", "inventory", "material", "demand"],
    },
    "procurement": {
        "display_name": "Procurement / Purchasing",
        "aliases": ["procurement", "purchasing", "buyer", "sourcing", "category manager", "supplier", "vendor"],
        "primary_fields": ["Procurement"],
        "adjacent_fields": ["Logistics & Supply Chain Management", "Finance, Controlling & Audit"],
        "title_keywords": ["procurement", "purchasing", "buyer", "sourcing", "category", "supplier", "vendor"],
    },
    "engineering": {
        "display_name": "Engineering / Technical Services",
        "aliases": ["engineering", "engineer", "technical services", "process engineer", "mechanical", "electrical", "chemical engineer", "maintenance"],
        "primary_fields": ["Engineering", "Production, Maintenance & Technical Services", "Applications Technology"],
        "adjacent_fields": ["Environment, Health & Safety", "Research & Development"],
        "title_keywords": ["engineer", "engineering", "technical", "process", "maintenance", "mechanical", "electrical", "chemical"],
    },
    "sales-marketing": {
        "display_name": "Marketing & Sales / Commercial",
        "aliases": ["sales", "marketing", "commercial", "business development", "key account", "account manager", "customer"],
        "primary_fields": ["Marketing & Sales", "Sales", "Commercial", "Business Development"],
        "adjacent_fields": ["Applications Technology", "Research & Development"],
        "title_keywords": ["sales", "marketing", "commercial", "business development", "account", "customer"],
    },
    "human-resources": {
        "display_name": "Human Resources / Talent",
        "aliases": ["hr", "human resources", "talent", "recruiting", "recruitment", "people", "learning", "payroll"],
        "primary_fields": ["Human Resources"],
        "adjacent_fields": ["Administration & Office Support"],
        "title_keywords": ["hr", "human resources", "talent", "recruit", "people", "learning", "payroll"],
    },
}


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


def job_matches_intent(job, intent):
    field = (job.get("job_field") or "").lower()
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    primary_fields = [f.lower() for f in intent.get("primary_fields", [])]
    adjacent_fields = [f.lower() for f in intent.get("adjacent_fields", [])]
    title_keywords = [k.lower() for k in intent.get("title_keywords", [])]

    if field in primary_fields or field in adjacent_fields:
        return True
    if any(keyword in title for keyword in title_keywords):
        return True
    if any(keyword in description for keyword in title_keywords):
        return True
    return False


def intent_score(job, intent, country=None, non_india=False):
    score = 0
    field = (job.get("job_field") or "").lower()
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    job_country = job.get("country")
    primary_fields = [f.lower() for f in intent.get("primary_fields", [])]
    adjacent_fields = [f.lower() for f in intent.get("adjacent_fields", [])]
    keywords = [k.lower() for k in intent.get("title_keywords", [])]

    if country and job_country == country:
        score += 5
    if non_india and job_country != "India":
        score += 5
    if field in primary_fields:
        score += 4
    if field in adjacent_fields:
        score += 2
    if any(k in title for k in keywords):
        score += 3
    if any(k in description for k in keywords):
        score += 1
    if job.get("date_posted"):
        score += 1
    return score


def sorted_intent_jobs(jobs, intent, country=None, non_india=False):
    filtered = [job for job in jobs if job_matches_intent(job, intent)]
    if country:
        filtered = [job for job in filtered if job.get("country") == country]
    if non_india:
        filtered = [job for job in filtered if job.get("country") != "India"]
    filtered.sort(key=lambda job: (intent_score(job, intent, country=country, non_india=non_india), job.get("date_posted", "")), reverse=True)
    return filtered


async def capture_api_key():
    env_api_key = os.getenv("BASF_SEARCH_API_KEY", "").strip()
    if env_api_key:
        print("✅ BASF Search API key loaded from BASF_SEARCH_API_KEY")
        return env_api_key

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
    for directory in ["countries", "regions", "data"]:
        if os.path.isdir(directory):
            shutil.rmtree(directory)

    os.makedirs("countries", exist_ok=True)
    os.makedirs("data/countries", exist_ok=True)
    os.makedirs("data/jobs", exist_ok=True)
    os.makedirs("data/agent", exist_ok=True)
    os.makedirs("data/intents", exist_ok=True)
    os.makedirs("data/apac/non-india/fields", exist_ok=True)


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
            "llm_instruction": "Use this file only after a candidate job was selected. Copy the BASF URL exactly from job.url; never construct job links.",
            "job": detail_job(job),
        }
        path = job_detail_path(job)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def write_non_india_apac_files(jobs, timestamp):
    non_india_jobs = [job for job in jobs if job.get("country") != "India"]
    payload = {
        "last_updated": timestamp,
        "scope": "non_india_apac",
        "total_active": len(non_india_jobs),
        "llm_instruction": (
            "Use this file when the user asks for APAC, other APAC countries, outside India, or non-India Asia roles. "
            "Filter by job_field/title/description locally. Copy each application link only from job.url. Never construct basf.jobs links."
        ),
        "jobs": [compact_job(job, include_preview=True, include_detail_path=True) for job in non_india_jobs],
    }
    with open("data/apac/non-india.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    grouped = defaultdict(list)
    for job in non_india_jobs:
        grouped[job.get("job_field", "Other")].append(job)

    for field, field_jobs in grouped.items():
        field_slug = slugify(field)
        field_payload = {
            "last_updated": timestamp,
            "scope": "non_india_apac_field",
            "job_field": field,
            "total_active": len(field_jobs),
            "llm_instruction": (
                "Use this small file for non-India APAC searches in this function. "
                "Copy application links only from job.url. Never construct basf.jobs links."
            ),
            "jobs": [compact_job(job, include_preview=True, include_detail_path=True) for job in field_jobs],
        }
        with open(f"data/apac/non-india/fields/{field_slug}.json", "w", encoding="utf-8") as f:
            json.dump(field_payload, f, ensure_ascii=False, indent=2)


def write_intent_files(jobs, grouped_by_country, timestamp):
    intent_routes = []
    for intent_slug, intent in INTENT_GROUPS.items():
        intent_dir = f"data/intents/{intent_slug}"
        country_dir = f"{intent_dir}/countries"
        os.makedirs(country_dir, exist_ok=True)

        all_matches = sorted_intent_jobs(jobs, intent)
        india_matches = sorted_intent_jobs(jobs, intent, country="India")
        non_india_matches = sorted_intent_jobs(jobs, intent, non_india=True)
        china_matches = sorted_intent_jobs(jobs, intent, country="China")

        def write_payload(path, scope, selected_jobs, extra=None):
            payload = {
                "last_updated": timestamp,
                "scope": scope,
                "intent": intent_slug,
                "display_name": intent["display_name"],
                "aliases": intent["aliases"],
                "primary_fields": intent["primary_fields"],
                "adjacent_fields": intent["adjacent_fields"],
                "title_keywords": intent["title_keywords"],
                "total_active": len(selected_jobs),
                "link_rule": "Application links must be copied only from job.url. Never construct, repair, shorten, or guess basf.jobs links.",
                "llm_instruction": (
                    "Use this intent-specific file when the user's request matches one of the aliases or keywords. "
                    "Default to India first unless the user names another country or asks for other APAC countries. "
                    "For details, fetch detail_path only for shortlisted roles."
                ),
                "jobs": [compact_job(job, include_preview=True, include_detail_path=True) for job in selected_jobs],
            }
            if extra:
                payload.update(extra)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        write_payload(f"{intent_dir}/all.json", "intent_all_asia_apac", all_matches)
        write_payload(f"{intent_dir}/india.json", "intent_india", india_matches)
        write_payload(f"{intent_dir}/non-india-apac.json", "intent_non_india_apac", non_india_matches)
        write_payload(f"{intent_dir}/china.json", "intent_china", china_matches)

        country_entries = []
        for country in sorted(grouped_by_country.keys(), key=country_sort_key):
            country_matches = sorted_intent_jobs(jobs, intent, country=country)
            if not country_matches:
                continue
            country_slug = slugify(country)
            country_path = f"{country_dir}/{country_slug}.json"
            write_payload(country_path, "intent_country", country_matches, {"country": country})
            country_entries.append({
                "country": country,
                "count": len(country_matches),
                "json_path": country_path,
                "json_url": f"{BASE_URL}/{country_path}",
            })

        route = {
            "intent": intent_slug,
            "display_name": intent["display_name"],
            "aliases": intent["aliases"],
            "primary_fields": intent["primary_fields"],
            "adjacent_fields": intent["adjacent_fields"],
            "title_keywords": intent["title_keywords"],
            "total_active": len(all_matches),
            "entrypoints": {
                "india_first": {
                    "json_path": f"{intent_dir}/india.json",
                    "json_url": f"{BASE_URL}/{intent_dir}/india.json",
                    "count": len(india_matches),
                    "use_when": "No country is named. Search India first.",
                },
                "non_india_apac": {
                    "json_path": f"{intent_dir}/non-india-apac.json",
                    "json_url": f"{BASE_URL}/{intent_dir}/non-india-apac.json",
                    "count": len(non_india_matches),
                    "use_when": "User asks for APAC, other APAC countries, or outside India.",
                },
                "china": {
                    "json_path": f"{intent_dir}/china.json",
                    "json_url": f"{BASE_URL}/{intent_dir}/china.json",
                    "count": len(china_matches),
                    "use_when": "User names China or asks for Chinese jobs.",
                },
                "all_asia_apac": {
                    "json_path": f"{intent_dir}/all.json",
                    "json_url": f"{BASE_URL}/{intent_dir}/all.json",
                    "count": len(all_matches),
                    "use_when": "Only when the user explicitly asks for all Asia/APAC results or country routing is insufficient.",
                },
                "countries": country_entries,
            },
        }
        intent_routes.append(route)

    intent_map = {
        "last_updated": timestamp,
        "scope": "intent_map",
        "llm_instruction": (
            "Use this file after data/agent-guide.json. Match the user's wording against aliases/title_keywords. "
            "Then fetch the smallest matching intent entrypoint. Do not load the full aggregate first. "
            "Application links must come only from job.url values. Never construct basf.jobs links."
        ),
        "default_country_logic": "If no country is named, use the intent entrypoint india_first. If the user asks for other APAC countries, use non_india_apac. If the user names China, use china. If another country is named, use the country entrypoint when present.",
        "intents": intent_routes,
    }
    with open("data/intent-map.json", "w", encoding="utf-8") as f:
        json.dump(intent_map, f, ensure_ascii=False, indent=2)


def write_agent_guide(timestamp):
    guide = {
        "last_updated": timestamp,
        "scope": "agent_guide",
        "purpose": "Minimal retrieval instructions for an LLM job agent using this BASF Asia/APAC feed.",
        "active_source": BASE_URL,
        "mandatory_behavior": [
            "If the user does not name a country, search India first. Do not ask whether they mean India or APAC.",
            "If the user asks for other APAC countries or outside India, use non-India APAC entrypoints.",
            "If the user names a country, search that country first when present in the feed.",
            "For job application links, copy only the exact job.url value from JSON. Never construct BASF links.",
            "Do not use LinkedIn, beBee, JoinImagine, external job boards, or generic web search for job results.",
            "Do not show internal GitHub/JSON/source paths to the end user.",
        ],
        "recommended_retrieval_order": [
            {
                "step": 1,
                "fetch": "data/agent-guide.json",
                "reason": "Read agent behavior and link rules.",
            },
            {
                "step": 2,
                "fetch": "data/intent-map.json",
                "reason": "Map user wording to a job intent and choose the smallest entrypoint.",
            },
            {
                "step": 3,
                "fetch": "data/intents/{intent}/india.json OR data/intents/{intent}/non-india-apac.json OR data/intents/{intent}/countries/{country}.json",
                "reason": "Use intent-specific files instead of large country or aggregate files.",
            },
            {
                "step": 4,
                "fetch": "job.detail_path only for shortlisted roles",
                "reason": "Retrieve detailed descriptions only for final candidates or comparisons.",
            },
        ],
        "link_validation": {
            "allowed": "A URL copied exactly from job.url and starting with https://basf.jobs/.",
            "not_allowed": [
                "Constructing /job-invite/ URLs from numbers or titles.",
                "Constructing /job/Job-Title/ URLs from titles.",
                "Using numbers in parentheses such as (007326) as job IDs.",
                "Using third-party URLs.",
                "Using country HTML pages as application links.",
            ],
            "reject_if_contains": INVALID_URL_TOKENS,
        },
        "entrypoints": {
            "intent_map": "data/intent-map.json",
            "routing": "data/llm-routing.json",
            "non_india_apac": "data/apac/non-india.json",
            "aggregate_last_resort": "jobs.json",
        },
    }
    with open("data/agent-guide.json", "w", encoding="utf-8") as f:
        json.dump(guide, f, ensure_ascii=False, indent=2)


def generate_agent_guide_page():
    intent_links = "<ul>\n"
    for slug, config in INTENT_GROUPS.items():
        intent_links += (
            f'<li><strong>{safe(config["display_name"])}</strong> '
            f'| <a href="data/intents/{slug}/india.json">India</a> '
            f'| <a href="data/intents/{slug}/non-india-apac.json">Non-India APAC</a> '
            f'| <a href="data/intents/{slug}/china.json">China</a> '
            f'| <a href="data/intents/{slug}/all.json">All Asia/APAC</a><br>'
            f'<small>Aliases: {safe(", ".join(config["aliases"][:12]))}</small></li>\n'
        )
    intent_links += "</ul>\n"

    guide_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BASF Jobs Asia/APAC – LLM Agent Guide</title>
</head>
<body>
  <h1>BASF Jobs Asia/APAC – LLM Agent Guide</h1>
  <p>This page is designed for AI agents. It explains how to retrieve BASF job data with a small context window and without hallucinating application links.</p>

  <h2>Core rule</h2>
  <p><strong>Application links must only be copied from the JSON field <code>job.url</code>. Never construct, repair, shorten, or guess BASF job links.</strong></p>

  <h2>Recommended agent flow</h2>
  <ol>
    <li>Fetch <a href="data/agent-guide.json">data/agent-guide.json</a>.</li>
    <li>Fetch <a href="data/intent-map.json">data/intent-map.json</a>.</li>
    <li>Match the user's wording against intent aliases and keywords.</li>
    <li>If no country is named, use the intent's India entrypoint first.</li>
    <li>If the user asks for other APAC countries, use the intent's Non-India APAC entrypoint.</li>
    <li>If the user names China, use the intent's China entrypoint.</li>
    <li>Fetch detail JSON only for shortlisted jobs or comparisons.</li>
  </ol>

  <h2>Important source files</h2>
  <ul>
    <li><a href="data/agent-guide.json">Agent guide JSON</a></li>
    <li><a href="data/intent-map.json">Intent map JSON</a></li>
    <li><a href="data/llm-routing.json">Country and field routing JSON</a></li>
    <li><a href="data/apac/non-india.json">Non-India APAC compact JSON</a></li>
    <li><a href="data/search-index.json">Search index JSON</a></li>
  </ul>

  <h2>Intent entrypoints</h2>
  {intent_links}

  <h2>Do not do this</h2>
  <ul>
    <li>Do not use LinkedIn, beBee, JoinImagine, or external job boards.</li>
    <li>Do not generate links such as <code>https://basf.jobs/job/Full-Stack-Engineer/</code>.</li>
    <li>Do not generate <code>/job-invite/</code> links from title numbers.</li>
    <li>Do not link a job to a country HTML page.</li>
  </ul>
</body>
</html>"""
    with open("agent-guide.html", "w", encoding="utf-8") as f:
        f.write(guide_html)


def write_json_files(jobs, grouped_by_country, grouped_by_country_field):
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    write_detail_files(jobs, timestamp)
    write_non_india_apac_files(jobs, timestamp)

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
                "link_rule": "Application links must be copied only from job.url. Never construct basf.jobs links.",
                "llm_instruction": (
                    "Use this small file for country-and-function-specific matching. "
                    "Descriptions are short previews. Fetch detail_path only for shortlisted roles. "
                    "Copy job URLs exactly from job.url. Never construct basf.jobs links."
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
            "link_rule": "Application links must be copied only from job.url. Never construct basf.jobs links.",
            "llm_instruction": (
                f"This file contains only BASF jobs in {country}. "
                "Descriptions are short previews. Fetch detail_path only for shortlisted roles. "
                "For role-specific searches, use data/intent-map.json or the field JSON files when available. "
                "Copy job URLs exactly from job.url and never generate basf.jobs links."
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
        "link_rule": "Application links must be copied only from job.url in JSON. Never construct basf.jobs links.",
        "llm_instruction": (
            "For most role searches, start with data/agent-guide.json and data/intent-map.json. "
            "Use this routing file when a country or field is not covered by an intent entrypoint. "
            "Default to India when no country is specified. Use wider Asia/APAC only as fallback or when explicitly requested. "
            "Copy BASF job URLs exactly from job.url."
        ),
        "countries": country_routes,
    }
    with open("data/llm-routing.json", "w", encoding="utf-8") as f:
        json.dump(routing_payload, f, ensure_ascii=False, indent=2)

    search_index = [search_index_job(job) for job in jobs]
    with open("data/search-index.jsonl", "w", encoding="utf-8") as f:
        for job in jobs:
            f.write(json.dumps(search_index_job(job), ensure_ascii=False) + "\n")
    with open("data/search-index.json", "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": timestamp,
            "scope": "search_index",
            "llm_instruction": "Use this compact array only as a fallback keyword index. Application links still must come from url exactly.",
            "jobs": search_index,
        }, f, ensure_ascii=False, indent=2)

    write_intent_files(jobs, grouped_by_country, timestamp)
    write_agent_guide(timestamp)

    aggregate_payload = {
        "last_updated": timestamp,
        "scope": "Asia/APAC",
        "total_active": len(jobs),
        "link_rule": "Application links must be copied only from job.url. Never construct basf.jobs links.",
        "llm_instruction": (
            "Large aggregate file. Prefer data/agent-guide.json, data/intent-map.json, country JSON files, "
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
  <p><a href="../index_lite.html">← Asia/APAC country index</a> | <a href="../agent-guide.html">Agent guide</a></p>
  <h1>BASF Job Openings {safe(country)}</h1>
  <p>Total: {len(country_jobs)} active position(s).</p>

  <h2>LLM usage</h2>
  <p>This page contains ONLY jobs in {safe(country)}. Use it when the user explicitly asks for {safe(country)}, or when {safe(country)} is the India-first/default country.</p>
  <p>For smaller and more reliable retrieval, use the JSON files first:</p>
  <ul>
    <li><a href="../data/agent-guide.json">Agent guide JSON</a></li>
    <li><a href="../data/intent-map.json">Intent map JSON</a></li>
    <li><a href="../data/countries/{country_slug}.json">Country JSON for {safe(country)}</a></li>
  </ul>
  <p><strong>Application links must only be copied from JSON <code>job.url</code>. Never construct job links from titles, IDs, or country pages.</strong></p>

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

    intent_rows = "<ul>\n"
    for slug, config in INTENT_GROUPS.items():
        intent_rows += (
            f'<li><strong>{safe(config["display_name"])}</strong> '
            f'| <a href="data/intents/{slug}/india.json">India</a> '
            f'| <a href="data/intents/{slug}/non-india-apac.json">Non-India APAC</a> '
            f'| <a href="data/intents/{slug}/china.json">China</a></li>\n'
        )
    intent_rows += "</ul>\n"

    shared_body = f"""
<h1>BASF Job Openings Asia/APAC</h1>
<p>Last updated: {safe(timestamp)}</p>
<p>Total: {len(jobs)} positions | {country_count} countries.</p>

<h2>Start here for AI agents</h2>
<p><strong>Recommended:</strong> fetch the agent guide first, then the intent map. Use intent-specific JSON files instead of loading the full job database.</p>
<ul>
  <li><a href="agent-guide.html">Agent guide HTML</a></li>
  <li><a href="data/agent-guide.json">Agent guide JSON</a></li>
  <li><a href="data/intent-map.json">Intent map JSON</a></li>
  <li><a href="data/llm-routing.json">Country and field routing JSON</a></li>
  <li><a href="data/apac/non-india.json">Non-India APAC compact JSON</a></li>
  <li><a href="data/search-index.json">Search index JSON</a></li>
</ul>
<p><strong>Critical link rule:</strong> application links must only be copied from JSON <code>job.url</code>. Never construct BASF job links from titles or IDs.</p>
<p>Default behavior for the agent: India first when no country is specified. If the user names a country, use that country first. Wider Asia/APAC is fallback only unless explicitly requested.</p>

<h2>Intent entrypoints</h2>
{intent_rows}

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
        print("⚠️ No BASF Search API key found. Skipping scrape and keeping existing feed files unchanged.")
        return

    print("✅ API Key found")
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
    print("✅ JSON routing, agent guide, intent files, search index, country, field, and detail files generated!")
    print(f"✅ jobs.json gespeichert — {len(jobs)} verified Asia/APAC Jobs!")

    generate_country_pages(grouped_by_country, grouped_by_country_field)
    print(f"✅ {len(grouped_by_country)} country pages generated!")

    generate_agent_guide_page()
    print("✅ agent-guide.html generated!")

    generate_index_pages(jobs, grouped_by_country)
    print("✅ index.html und index_lite.html saved!")


asyncio.run(scrape_jobs())
