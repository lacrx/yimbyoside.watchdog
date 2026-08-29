"""AWS Lambda handler for watchdog pipeline.

Wraps existing pipeline modules. Dispatches based on event['phase']:
  - preflight: health-check agency endpoints
  - scrape: fetch documents for one or all agencies
  - process: merge + rollup structured data
  - full: preflight → scrape all → process (sequential)

Environment:
  WATCHDOG_S3_BUCKET  — S3 bucket for pipeline data
  WATCHDOG_DATA_DIR   — local data directory (/tmp/data in Lambda)
"""

import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path

os.environ.setdefault("WATCHDOG_DATA_DIR", "/tmp/data")

sys.path.insert(0, os.path.dirname(__file__))

from civic_utils import load_agencies, DATA_DIR
from lib.storage import sync_dir_to_s3, write_json, read_json, is_s3

BUCKET = os.environ.get("WATCHDOG_S3_BUCKET", "")

PLATFORM_SCRAPER = {
    "legistar_html": "scrapers.oceanside",
    "legistar_odata": "scrapers.sdcounty",
    "escribe": "scrapers.sandag",
    "custom_html": "scrapers.nctd",
    "coastal_api": "scrapers.coastal",
    "granicus": "scrapers.granicus",
    "civicplus": "scrapers.civicplus",
    "civicclerk": "scrapers.civicclerk",
    "carlsbad_cms": "scrapers.carlsbad",
    "swagit": "scrapers.swagit",
    "solana_drupal": "scrapers.solana_beach",
    "municode": "scrapers.municode",
    "primegov": "scrapers.primegov",
    "lemon_grove_events": "scrapers.lemon_grove",
    "santee_cms": "scrapers.santee",
    "elcajon_cms": "scrapers.elcajon",
    "laserfiche": "scrapers.laserfiche",
}


def ensure_data_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "structured").mkdir(exist_ok=True)
    (DATA_DIR / "pipeline").mkdir(exist_ok=True)


def run_preflight():
    """Run preflight health checks, write results to S3."""
    import pipeline_preflight

    ensure_data_dirs()

    health_file = DATA_DIR / "preflight-health.json"
    health_file.parent.mkdir(parents=True, exist_ok=True)

    pipeline_preflight.HEALTH_FILE = health_file

    args = Namespace(agency=None, verbose=False)
    agencies = load_agencies(enabled_only=True)
    health = pipeline_preflight.load_health()

    results = {}
    for slug, cfg in agencies.items():
        if cfg.get("local_only"):
            results[slug] = {"ok": None, "detail": "local_only (skipped in Lambda)"}
            continue
        ok, detail = pipeline_preflight.check_agency(slug, cfg, health, verbose=False)
        results[slug] = {"ok": ok, "detail": detail}

    pipeline_preflight.save_health(health)

    if BUCKET:
        sync_dir_to_s3(str(health_file.parent), "pipeline")

    ok_count = sum(1 for r in results.values() if r["ok"] is True)
    fail_count = sum(1 for r in results.values() if r["ok"] is False)

    return {
        "status": "abort" if ok_count == 0 and fail_count > 0 else "ok",
        "healthy": ok_count,
        "failing": fail_count,
        "results": results,
    }


def run_scrape(agency_slug=None):
    """Scrape one agency or all enabled agencies."""
    import importlib

    ensure_data_dirs()

    agencies = load_agencies(enabled_only=True)
    if agency_slug:
        if agency_slug not in agencies:
            return {"error": f"Unknown agency: {agency_slug}"}
        agencies = {agency_slug: agencies[agency_slug]}

    results = {}
    new_docs = []

    for slug, cfg in agencies.items():
        if cfg.get("local_only"):
            results[slug] = {"status": "skipped", "reason": "local_only (WAF-blocked from Lambda)"}
            continue

        platform = cfg.get("platform", "")
        module_name = PLATFORM_SCRAPER.get(platform)
        if not module_name:
            results[slug] = {"status": "skipped", "reason": f"Unknown platform: {platform}"}
            continue

        agency_dir = DATA_DIR / slug
        agency_dir.mkdir(parents=True, exist_ok=True)
        (agency_dir / "documents").mkdir(exist_ok=True)
        (agency_dir / "meetings").mkdir(exist_ok=True)

        doc_count_before = len(list((agency_dir / "documents").glob("*.txt")))

        try:
            mod = importlib.import_module(module_name)
            lookback = cfg.get("lookback_months")
            years = max(1, int(lookback / 12) + 1) if lookback else 1
            deep = cfg.get("deep_fetch", False)

            args = Namespace(years=years, force=False, deep=deep, agency=slug)
            mod.cmd_fetch(args)

            if platform == "escribe":
                from transforms.split_packets import split_sandag_packet, has_split_marker, write_split_marker, MIN_PACKET_SIZE
                for pkt in (agency_dir / "documents").glob("*-agenda-packet.txt"):
                    if pkt.stat().st_size < MIN_PACKET_SIZE or has_split_marker(pkt):
                        continue
                    items = split_sandag_packet(pkt.read_text())
                    if len(items) < 2:
                        continue
                    stem = pkt.stem
                    item_files = []
                    id_counts = {}
                    for item_id, item_text in items:
                        if item_id == "preamble":
                            continue
                        safe_id = item_id.replace(".", "-")
                        id_counts[safe_id] = id_counts.get(safe_id, 0) + 1
                        if id_counts[safe_id] > 1:
                            safe_id = f"{safe_id}-{chr(96 + id_counts[safe_id])}"
                        item_path = pkt.parent / f"{stem}-item-{safe_id}.txt"
                        item_path.write_text(item_text)
                        item_files.append(item_path)
                    write_split_marker(pkt, len(items), item_files)

            doc_count_after = len(list((agency_dir / "documents").glob("*.txt")))
            new_count = doc_count_after - doc_count_before

            if BUCKET and new_count > 0:
                sync_dir_to_s3(str(agency_dir / "documents"), f"raw/{slug}/documents")
                sync_dir_to_s3(str(agency_dir / "meetings"), f"raw/{slug}/meetings")

            if new_count > 0:
                for f in sorted((agency_dir / "documents").glob("*.txt"))[-new_count:]:
                    new_docs.append(f"{slug}/documents/{f.name}")

            results[slug] = {"status": "ok", "new_docs": new_count}
        except Exception as e:
            results[slug] = {"status": "error", "error": str(e)[:500]}

    if BUCKET and new_docs:
        write_json("pending-extraction.json", {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "new_docs": new_docs,
        }, prefix="pipeline")

    return {"results": results, "total_new_docs": len(new_docs)}


def run_process():
    """Run merge + rollup on structured data."""
    ensure_data_dirs()

    structured_dir = DATA_DIR / "structured"
    structured_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    try:
        from transforms import meeting_merge
        meeting_merge.main()
        results["merge"] = "ok"
    except Exception as e:
        results["merge"] = f"error: {e}"

    try:
        from transforms import monthly_rollup
        monthly_rollup.main()
        results["rollup"] = "ok"
    except Exception as e:
        results["rollup"] = f"error: {e}"

    if BUCKET:
        sync_dir_to_s3(str(structured_dir), "structured")

    return results


def handler(event, context):
    """Lambda entry point."""
    phase = event.get("phase", "full")
    print(f"[watchdog] Starting phase: {phase}")

    if phase == "preflight":
        return run_preflight()

    elif phase == "scrape":
        agency = event.get("agency")
        return run_scrape(agency)

    elif phase == "process":
        return run_process()

    elif phase == "full":
        preflight = run_preflight()
        print(f"[watchdog] Preflight: {preflight['healthy']} healthy, {preflight['failing']} failing")

        if preflight["status"] == "abort":
            return {"phase": "full", "status": "aborted", "reason": "All agencies failing"}

        scrape = run_scrape()
        print(f"[watchdog] Scrape: {scrape['total_new_docs']} new docs")

        process = run_process()
        print(f"[watchdog] Process: {process}")

        return {
            "phase": "full",
            "status": "ok",
            "preflight": preflight,
            "scrape": scrape,
            "process": process,
        }

    else:
        return {"error": f"Unknown phase: {phase}"}
