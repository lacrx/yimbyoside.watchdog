#!/usr/bin/env python3
"""
Swagit/Granicus video portal scraper — paginated HTML listings with agenda PDFs.

Handles sites on carlsbadca.new.swagit.com and similar Swagit portals.
Each body is a slug under /views/{view_id}/{body_slug}. Listings are
paginated (?page=N), 10 meetings per page. Agenda PDFs live at
/videos/{video_id}/agenda (302 redirect to granicus CDN).

Usage:
    python swagit.py fetch --agency carlsbad [--years N] [--deep]
    python swagit.py list --agency carlsbad

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup, Comment

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, make_meeting_id,
    cmd_list_meetings, rebuild_doc_index, log_discovery,
)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def _body_from_title(title):
    """Extract body name from Swagit meeting title like 'Housing Commission Meeting'."""
    body = re.sub(r'\s+(?:Regular |Special )?(?:Meeting|Hearing|Session|Workshop)\s*$', '', title, flags=re.I).strip()
    return body or None


def get_agency_config(slug):
    cfg = load_agencies(enabled_only=False).get(slug)
    if not cfg:
        print(f"Agency '{slug}' not found in agencies.yaml")
        sys.exit(1)
    return cfg


def fetch_page(url):
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_body_tabs(html):
    """Extract body name -> slug from navigation tabs."""
    soup = BeautifulSoup(html, "lxml")
    tabs = {}
    for a in soup.select("ul.nav-tabs-swagit a[href]"):
        href = a["href"]
        m = re.search(r"/views/\d+/(.+)$", href)
        if m:
            tabs[a.get_text(strip=True)] = m.group(1)
    return tabs


def parse_meeting_rows(html):
    """Parse meetings from video-table. Returns list of dicts."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="video-table")
    if not table:
        return []

    meetings = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        link = cells[0].find("a", href=True)
        if not link:
            continue

        href = link["href"]
        video_match = re.search(r"/videos/(\d+)", href)
        if not video_match:
            continue

        video_id = video_match.group(1)
        title = link.get_text(strip=True)
        date_text = cells[1].get_text(strip=True)

        try:
            dt = datetime.strptime(date_text, "%b %d, %Y")
        except ValueError:
            continue

        duration = cells[2].get_text(strip=True) if len(cells) > 2 else ""

        # Agenda link may be in a commented-out column
        has_agenda = False
        for comment in tr.find_all(string=lambda t: isinstance(t, Comment)):
            if "/agenda" in comment:
                has_agenda = True
                break
        if not has_agenda:
            for a in tr.find_all("a", href=True):
                if "/agenda" in a["href"]:
                    has_agenda = True
                    break

        meetings.append({
            "video_id": video_id,
            "title": title,
            "date": dt,
            "duration": duration,
            "has_agenda": has_agenda,
        })

    return meetings


def get_last_page(html):
    """Extract total page count from pagination."""
    soup = BeautifulSoup(html, "lxml")
    pag = soup.find("ul", class_="pagination")
    if not pag:
        return 1
    max_page = 1
    for li in pag.find_all("li"):
        a = li.find("a", href=True)
        text = (a or li.find("span") or li).get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))
    return max_page


def paginate_body(base_url, view_id, body_slug, cutoff_date):
    """Yield all meetings for a body, paginating until cutoff or end."""
    page = 1
    while True:
        url = f"{base_url}/views/{view_id}/{body_slug}"
        if page > 1:
            url += f"?page={page}"

        html = fetch_page(url)
        meetings = parse_meeting_rows(html)

        if not meetings:
            break

        past_cutoff = False
        for m in meetings:
            if m["date"] < cutoff_date:
                past_cutoff = True
                break
            yield m

        if past_cutoff:
            break

        last_page = get_last_page(html)
        if page >= last_page:
            break

        page += 1
        time.sleep(0.5)


def download_agenda_pdf(base_url, video_id, dest_path):
    """Download agenda PDF via /videos/{id}/agenda redirect."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True

    url = f"{base_url}/videos/{video_id}/agenda"
    try:
        resp = SESSION.get(url, timeout=60, allow_redirects=True, stream=True)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()

        content = resp.content
        if b"%PDF" not in content[:20]:
            return False

        dest_path.write_bytes(content)
        return True
    except Exception as e:
        print(f"    Agenda download failed for {video_id}: {e}")
        return False


def cmd_fetch(args):
    slug = args.agency
    cfg = get_agency_config(slug)
    prefix = cfg.get("swagit_prefix", slug[:2])

    base_url = cfg["base_url"]
    view_id = cfg.get("swagit_view_id", "295")
    body_slugs = cfg.get("swagit_bodies", {})

    if not body_slugs:
        # Auto-discover from tabs on first page
        html = fetch_page(f"{base_url}/views/{view_id}")
        body_slugs = parse_body_tabs(html)
        if not body_slugs:
            print(f"No body slugs found for {slug}")
            return

    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(data_dir / "state.json") or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching {cfg.get('name', slug)} meetings from Swagit...")
    new_count = 0
    doc_count = 0

    bodies_config = cfg.get("bodies", [])
    archive_slugs = cfg.get("swagit_archive_slugs", {})

    for body_name, body_slug in body_slugs.items():
        if bodies_config and body_name not in bodies_config:
            continue

        print(f"\n  {body_name}...")
        meeting_count = 0

        # Build list of slugs to scrape: main slug first, then archive slugs
        slugs_to_scrape = [body_slug]
        if body_name in archive_slugs:
            for archive_slug in archive_slugs[body_name]:
                # Extract year from slug (e.g. "city-council-2021" -> 2021)
                year_match = re.search(r"(\d{4})(?:-(\d{4}))?$", archive_slug)
                if year_match:
                    slug_year = int(year_match.group(2) or year_match.group(1))
                    if cutoff.year <= slug_year:
                        slugs_to_scrape.append(archive_slug)
                else:
                    slugs_to_scrape.append(archive_slug)

        for current_slug in slugs_to_scrape:
            for m in paginate_body(base_url, view_id, current_slug, cutoff):
                meeting_count += 1
                dt = m["date"]
                # For mixed-body slugs, derive body from meeting title
                effective_body = _body_from_title(m["title"]) or body_name
                mid = make_meeting_id(prefix, effective_body, dt)
                mdir = meetings_dir / mid
                mdir.mkdir(exist_ok=True)

                meeting_file = mdir / "meeting.json"
                if not meeting_file.exists():
                    meta = {
                        "body": effective_body,
                        "date": dt.strftime("%m/%d/%Y"),
                        "title": f"{effective_body} — {dt.strftime('%b %d, %Y')}",
                        "id": mid,
                        "source": "swagit",
                        "agency": slug,
                        "video_id": m["video_id"],
                        "video_url": f"{base_url}/videos/{m['video_id']}",
                        "duration": m["duration"],
                    }
                    save_json(meeting_file, meta)
                    new_count += 1
                    print(f"    NEW: {effective_body} — {dt.strftime('%Y-%m-%d')}")

                if mid in state["meetings"] and not getattr(args, "force", False):
                    continue

                pdf_name = f"{mid}-agenda.pdf"
                pdf_path = docs_dir / pdf_name
                if download_agenda_pdf(base_url, m["video_id"], pdf_path):
                    text = extract_text(pdf_path)
                    if text:
                        doc_count += 1
                    time.sleep(0.3)

                state["meetings"][mid] = {
                    "fetched": datetime.now().isoformat(),
                    "body": effective_body,
                    "video_id": m["video_id"],
                }

        print(f"    {meeting_count} meetings")
        time.sleep(0.5)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(data_dir / "state.json", state)
    log_discovery(slug, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="Swagit/Granicus video portal scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")
    p_fetch.add_argument("--force", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", required=True)

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
