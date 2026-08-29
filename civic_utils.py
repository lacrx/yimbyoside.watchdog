#!/usr/bin/env python3
"""Shared utilities for civic monitoring scrapers."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml


def _resolve_bin(name):
    """Find a working binary, preferring /usr/bin when linuxbrew is broken."""
    system_path = Path(f"/usr/bin/{name}")
    if system_path.exists():
        try:
            subprocess.run([str(system_path), "--version"],
                           capture_output=True, timeout=5)
            return str(system_path)
        except Exception:
            pass
    return shutil.which(name) or name


PDFTOTEXT_BIN = _resolve_bin("pdftotext")
CURL_BIN = _resolve_bin("curl")


ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WATCHDOG_DATA_DIR", str(REPO_ROOT / "data")))
AGENCIES_FILE = REPO_ROOT / "agencies.yaml"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"

_agencies_cache = None


def load_agencies(enabled_only=True):
    """Load agency registry from agencies.yaml. Returns dict keyed by slug."""
    global _agencies_cache
    if _agencies_cache is None:
        with open(AGENCIES_FILE) as f:
            _agencies_cache = yaml.safe_load(f).get("agencies", {})
    if enabled_only:
        return {k: v for k, v in _agencies_cache.items() if v.get("enabled", True)}
    return dict(_agencies_cache)


def agency_data_dir(slug):
    """Return data directory for an agency: data/{slug}/"""
    return DATA_DIR / slug


def agency_docs_dir(slug):
    """Return documents directory for an agency: data/{slug}/documents/"""
    return DATA_DIR / slug / "documents"


def agency_meetings_dir(slug):
    """Return meetings directory for an agency: data/{slug}/meetings/"""
    return DATA_DIR / slug / "meetings"


def all_docs_dirs(enabled_only=True):
    """Return list of existing document directories across all agencies."""
    dirs = []
    for slug in load_agencies(enabled_only=enabled_only):
        d = agency_docs_dir(slug)
        if d.exists():
            dirs.append(d)
    return dirs


def all_meetings_dirs(enabled_only=True):
    """Return list of existing meeting directories across all agencies."""
    dirs = []
    for slug in load_agencies(enabled_only=enabled_only):
        d = agency_meetings_dir(slug)
        if d.exists():
            dirs.append(d)
    return dirs


def download_pdf(url, dest_path, headers=None, verify=True):
    """Download a PDF. Skips if already exists and non-empty."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        resp = requests.get(url, timeout=60, headers=hdrs, allow_redirects=True,
                            verify=verify)
        resp.raise_for_status()
        if b"%PDF" in resp.content[:10]:
            dest_path.write_bytes(resp.content)
            return True
        return False
    except Exception as e:
        print(f"  download failed: {e}")
        return False


def extract_text(pdf_path):
    """Extract text from PDF using pdftotext. Caches as .txt alongside."""
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 0:
        return txt_path.read_text()
    try:
        result = subprocess.run(
            [PDFTOTEXT_BIN, "-layout", str(pdf_path), str(txt_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and txt_path.exists():
            return txt_path.read_text()
    except Exception as e:
        print(f"  pdftotext failed: {e}")
    return ""




def claude_api_call(client, max_retries=20, **kwargs):
    """Call Claude API with aggressive retry on rate limits.

    Retries with exponential backoff up to 10 minutes between attempts.
    Designed to survive token quota exhaustion — waits for refresh.
    """
    import anthropic

    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = min(60 * (2 ** attempt), 600)
            print(f"  Rate limited (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                wait = min(30 * (2 ** attempt), 300)
                print(f"  API overloaded (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"API call failed after {max_retries} retries")


def claude_local_call(prompt, system=None, timeout=300):
    """Call Claude via claude -p (subscription, no API cost).

    Returns the response text, or None on failure.
    Mirrors claude_api_call() but for local/subscription use.
    """
    full_prompt = ""
    if system:
        full_prompt = f"SYSTEM CONTEXT:\n{system}\n\n---\n\n"
    full_prompt += prompt

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            print(f"  claude -p error: {result.stderr[:200]}")
        return None
    except FileNotFoundError:
        print("  claude CLI not found.")
        return None
    except subprocess.TimeoutExpired:
        print(f"  claude -p timed out ({timeout}s)")
        return None


def parse_escribe_date(start_field):
    """Parse /Date(milliseconds)/ format from eScribe API."""
    m = re.search(r"/Date\((\d+)\)/", start_field)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000)
    return None


def safe_filename(name, max_len=80):
    """Clean a string for use in filenames."""
    name = re.sub(r'[^\w\s\-.]', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:max_len] or "document"


def make_meeting_id(prefix, body, dt):
    """Generate slug-based meeting ID: {prefix}-{body_slug}-{YYYYMMDD}."""
    body_slug = re.sub(r'[^a-z0-9]+', '-', body.lower()).strip('-')[:30]
    date_part = dt.strftime('%Y%m%d') if isinstance(dt, datetime) else dt
    return f"{prefix}-{body_slug}-{date_part}"


def make_meeting_id_hash(prefix, body, date_str):
    """Generate hash-based meeting ID for platforms with non-unique body+date."""
    return hashlib.md5(f"{prefix}-{body}-{date_str}".encode()).hexdigest()[:12]


def cmd_list_meetings(slug):
    """Generic list command — prints all meetings for an agency."""
    meetings_dir = agency_meetings_dir(slug)
    if not meetings_dir.exists():
        print("No meetings directory found.")
        return

    meetings = []
    for mdir in sorted(meetings_dir.iterdir()):
        mf = mdir / "meeting.json"
        if mf.exists():
            meetings.append(load_json(mf))

    meetings.sort(key=lambda m: m.get("date", ""), reverse=True)
    for m in meetings:
        print(f"  {m.get('date', '?'):12s}  {m.get('body', '?'):40s}  {m.get('id', '?')}")
    print(f"\n  {len(meetings)} meetings total")


def load_json(path):
    """Load JSON file, return empty dict on failure."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    """Save data as formatted JSON."""
    Path(path).write_text(json.dumps(data, indent=2, default=str))


def normalize_date(date_str):
    """Normalize date string to YYYY-MM-DD."""
    if not date_str:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(date_str)):
        return str(date_str)
    if re.match(r"^\d{4}-\d{2}$", str(date_str)):
        return str(date_str) + "-01"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", str(date_str))
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _match_meeting_id(filename, meeting_ids):
    """Find which meeting_id a document filename belongs to (prefix match)."""
    for mid in sorted(meeting_ids, key=len, reverse=True):
        if filename.startswith(mid + "-") or filename == mid + ".txt":
            return mid
    return None


def _date_from_meeting_id(mid):
    """Extract YYYY-MM-DD from meeting IDs like cb-body-20260623 or ccc-2022-02."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})$", mid)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4}-\d{2})$", mid)
    if m:
        return m.group(1) + "-01"
    return ""


_TITLE_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})"
)
_TITLE_DATE_RE2 = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _date_from_title(title):
    """Parse date from meeting title like 'City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    if not title:
        return ""
    m = _TITLE_DATE_RE.search(title)
    if m:
        months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                  "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        mon = months.get(m.group(1), 0)
        return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    m = _TITLE_DATE_RE2.search(title)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _body_from_title(title):
    """Extract body name from title like 'Regular City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    if not title:
        return ""
    # Strip time and date suffix
    body = re.split(r"\s+\d{1,2}:\d{2}\s*[ap]\.?m\.?", title, flags=re.IGNORECASE)[0]
    # Strip prefixes like "Regular", "Special"
    body = re.sub(r"^(Regular|Special|Joint|Adjourned)\s+", "", body, flags=re.IGNORECASE)
    return body.strip(" -") or ""


def rebuild_doc_index(slug, state, docs_dir):
    """Build doc-index.json mapping document filenames to meeting dates.

    Called at end of each scraper's cmd_fetch(). Uses meeting_id prefix
    matching against on-disk .txt files.

    Date priority: extraction-derived date (from structured/*.json) is primary
    as meeting_date. Legistar/scraper date kept as filed_date for provenance.
    Falls back to filed_date when no extraction exists.
    """
    meetings = state.get("meetings", {})
    meeting_ids = list(meetings.keys())
    documents = {}

    docs_path = Path(docs_dir)
    if not docs_path.exists():
        return 0

    structured_dir = DATA_DIR / "structured"
    extraction_dates = _load_extraction_dates(structured_dir)

    for txt_file in docs_path.glob("*.txt"):
        mid = _match_meeting_id(txt_file.name, meeting_ids)
        if mid and mid in meetings:
            m = meetings[mid]
            filed_date = normalize_date(m.get("date", ""))
            if not filed_date:
                filed_date = _date_from_title(m.get("title", ""))
            if not filed_date:
                filed_date = _date_from_meeting_id(mid)
            if not filed_date:
                continue

            stem = txt_file.name[:-4] if txt_file.name.endswith(".txt") else txt_file.name
            ext_date = extraction_dates.get(stem, "")

            entry = {
                "meeting_date": ext_date if ext_date else filed_date,
                "filed_date": filed_date,
                "body": m.get("body", "") or _body_from_title(m.get("title", "")),
            }
            documents[txt_file.name] = entry

    index = {
        "_generated": datetime.now().isoformat(timespec="seconds"),
        "documents": documents,
    }

    index_path = agency_data_dir(slug) / "doc-index.json"
    save_json(index_path, index)
    return len(documents)


def _load_extraction_dates(structured_dir):
    """Load date fields from extraction JSONs. Returns {stem: YYYY-MM-DD}."""
    dates = {}
    if not structured_dir.exists():
        return dates
    for json_file in structured_dir.glob("*.json"):
        if json_file.name in ("extraction-state.json", "meetings-state.json", "document-dates.json"):
            continue
        try:
            record = json.loads(json_file.read_text())
            if isinstance(record, dict) and "date" in record:
                d = record["date"]
                if d and re.match(r"^\d{4}-\d{2}-\d{2}$", str(d)):
                    dates[json_file.stem] = str(d)
        except (json.JSONDecodeError, Exception):
            continue
    return dates


DISCOVERY_LOG = DATA_DIR / "pipeline" / "discovery.jsonl"


def log_discovery(agency, meetings_found=0, meetings_new=0, docs_new=0, docs_unchanged=0):
    """Log scraper discovery stats for cost projection."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agency": agency,
        "meetings_found": meetings_found,
        "meetings_new": meetings_new,
        "docs_new": docs_new,
        "docs_unchanged": docs_unchanged,
    }
    DISCOVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
