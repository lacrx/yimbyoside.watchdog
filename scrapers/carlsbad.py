#!/usr/bin/env python3
"""
Carlsbad scraper — CivicPlus CMS document-folder pages behind Akamai WAF.

City Council agendas: /city-hall/meetings-agendas (folder links per meeting)
Boards/Commissions: /city-hall/meetings-agendas/boards-commissions/{slug} (same pattern)

WAF bypass: Akamai fingerprints TLS — Python requests gets 403, but curl
passes. All HTTP is done via subprocess curl. PDF downloads need Referer.
If Akamai tightens further, this breaks and preflight detects it.

Usage:
    python carlsbad.py fetch [--years N] [--deep]
    python carlsbad.py list

Requires: curl, beautifulsoup4, lxml
"""

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bs4 import BeautifulSoup

from civic_utils import extract_text, save_json, load_json, agency_data_dir, load_agencies, cmd_list_meetings, rebuild_doc_index, log_discovery, CURL_BIN

SLUG = "carlsbad"
BASE_URL = "https://www.carlsbadca.gov"

CURL_BASE = [
    CURL_BIN, "-s", "--compressed", "--max-time", "30",
    "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-US,en;q=0.5",
    "-H", "Connection: keep-alive",
    "-H", "Upgrade-Insecure-Requests: 1",
    "-H", "Sec-Fetch-Dest: document",
    "-H", "Sec-Fetch-Mode: navigate",
    "-H", "Sec-Fetch-Site: none",
    "-H", "Sec-Fetch-User: ?1",
]


DATE_PATTERNS = [
    (r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
     ["%B %d, %Y", "%B %d %Y"]),
    (r"(\d{1,2}-\d{1,2}-\d{4})", ["%m-%d-%Y"]),
]


def parse_date(text):
    for pattern, fmts in DATE_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            s = m.group(1)
            for fmt in fmts:
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
    return None


def meeting_id(body, dt):
    body_slug = re.sub(r'[^a-z0-9]+', '-', body.lower()).strip('-')[:30]
    return f"cb-{body_slug}-{dt.strftime('%Y%m%d')}"


def curl_get(url, extra_headers=None, timeout=30):
    """Fetch URL via subprocess curl (bypasses TLS fingerprinting)."""
    cmd = list(CURL_BASE)
    cmd[cmd.index("30")] = str(timeout)
    if extra_headers:
        for k, v in extra_headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr[:200]}")
    return result.stdout


def curl_download(url, dest_path, referer=None, timeout=60):
    """Download file via curl. Returns True on success."""
    cmd = [
        CURL_BIN, "-s", "--compressed", "--max-time", str(timeout),
        "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "-H", "Accept: application/pdf,*/*",
        "-H", "Connection: keep-alive",
        "-H", "Upgrade-Insecure-Requests: 1",
        "-H", "Sec-Fetch-Dest: document",
        "-H", "Sec-Fetch-Mode: navigate",
        "-H", "Sec-Fetch-Site: same-origin",
        "-o", str(dest_path),
    ]
    if referer:
        ref_url = f"{BASE_URL}{referer}" if referer.startswith("/") else referer
        cmd.extend(["-H", f"Referer: {ref_url}"])
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if result.returncode != 0:
        return False
    if not dest_path.exists() or dest_path.stat().st_size == 0:
        dest_path.unlink(missing_ok=True)
        return False
    # Verify it's actually a PDF
    header = dest_path.read_bytes()[:10]
    if b"%PDF" not in header:
        dest_path.unlink(missing_ok=True)
        return False
    return True


def fetch_page(path):
    url = f"{BASE_URL}{path}" if path.startswith("/") else path
    html = curl_get(url)
    if "Access Denied" in html[:500]:
        raise RuntimeError(f"WAF blocked: {url}")
    return html


def parse_folder_links(html, cutoff_date=None):
    soup = BeautifulSoup(html, "lxml")
    folders = soup.find_all("a", href=re.compile(r"-folder-"))
    meetings = []

    for a in folders:
        text = a.get_text(strip=True)
        href = a["href"]
        dt = parse_date(text)
        if not dt:
            continue
        if cutoff_date and dt < cutoff_date:
            continue

        meeting_type = "Special" if "special" in text.lower() else "Regular"
        meetings.append({
            "date": dt,
            "folder_path": href,
            "title": text,
            "meeting_type": meeting_type,
        })

    return meetings


def parse_folder_pdfs(html):
    soup = BeautifulSoup(html, "lxml")
    pdfs = soup.find_all("a", href=re.compile(r"showpublisheddocument"))
    docs = []

    skip_names = {"approved annual calendar", "learn what goes on",
                  "city council meeting calendar"}

    for a in pdfs:
        text = a.get_text(strip=True)
        href = a["href"]
        if any(s in text.lower() for s in skip_names):
            continue

        text_lower = text.lower()
        if "action agenda" in text_lower:
            doc_type = "action_agenda"
        elif "agenda packet" in text_lower:
            doc_type = "packet"
        elif "agenda" in text_lower:
            doc_type = "agenda"
        elif "minute" in text_lower:
            doc_type = "minutes"
        elif "correspondence" in text_lower:
            doc_type = "correspondence"
        else:
            doc_type = "attachment"

        docs.append({"url": href, "type": doc_type, "name": text})

    return docs


def cmd_fetch(args):
    data_dir = agency_data_dir(SLUG)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching City of Carlsbad meetings...")
    new_count = 0
    doc_count = 0

    cfg = load_agencies(enabled_only=False).get(SLUG, {})
    body_pages = cfg.get("body_pages", {})
    if not body_pages:
        print(f"No body_pages configured for {SLUG} in agencies.yaml")
        sys.exit(1)

    for body, path in body_pages.items():
        print(f"\n  {body}...")
        try:
            html = fetch_page(path)
        except RuntimeError as e:
            print(f"    {e}")
            continue
        except Exception as e:
            print(f"    Failed: {e}")
            continue

        folder_meetings = parse_folder_links(html, cutoff_date=cutoff)
        print(f"    {len(folder_meetings)} meetings")

        for meeting in folder_meetings:
            dt = meeting["date"]
            mid = meeting_id(body, dt)
            mdir = meetings_dir / mid
            mdir.mkdir(exist_ok=True)

            meeting_file = mdir / "meeting.json"
            if not meeting_file.exists():
                meta = {
                    "body": body,
                    "date": dt.strftime("%m/%d/%Y"),
                    "title": f"{body} — {dt.strftime('%b %d, %Y')} ({meeting['meeting_type']})",
                    "id": mid,
                    "source": "carlsbad_cms",
                    "agency": SLUG,
                    "folder_url": meeting["folder_path"],
                }
                save_json(meeting_file, meta)
                new_count += 1
                print(f"    NEW: {body} — {dt.strftime('%Y-%m-%d')}")

            if mid in state["meetings"] and not args.deep:
                continue

            # Fetch folder page for PDFs
            try:
                folder_html = fetch_page(meeting["folder_path"])
            except Exception as e:
                print(f"    Failed folder: {e}")
                continue

            folder_pdfs = parse_folder_pdfs(folder_html)

            for doc in folder_pdfs:
                doc_type = doc["type"]
                if not args.deep and doc_type not in ("agenda", "action_agenda", "minutes"):
                    continue

                safe_name = re.sub(r'[^\w\s\-.]', '', doc["name"])
                safe_name = re.sub(r'\s+', '_', safe_name).strip('_')[:60]
                dest = docs_dir / f"{mid}-{doc_type}-{safe_name}.pdf"

                if dest.exists():
                    continue

                pdf_url = f"{BASE_URL}{doc['url']}" if doc["url"].startswith("/") else doc["url"]
                if curl_download(pdf_url, dest, referer=meeting["folder_path"]):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.5)
                else:
                    print(f"    WAF blocked PDF: {doc['name'][:50]}")

            state["meetings"][mid] = {
                "fetched": datetime.now().isoformat(),
                "body": body,
            }
            time.sleep(1)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index("carlsbad", state, docs_dir)
    save_json(state_file, state)
    log_discovery("carlsbad", meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(SLUG)


def main():
    parser = argparse.ArgumentParser(description="Carlsbad CMS scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")

    sub.add_parser("list")

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
