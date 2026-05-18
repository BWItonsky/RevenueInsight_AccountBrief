#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

import openpyxl

BASE_URL = "https://prod.weatherfx.com/api"

STOPWORDS = {
    "the",
    "and",
    "company",
    "companies",
    "corporation",
    "corp",
    "inc",
    "llc",
    "ltd",
    "lp",
    "usa",
    "us",
    "north",
    "america",
    "american",
    "co",
}

HEALTH_TERMS = [
    "air quality",
    "allerg",
    "asthma",
    "bug",
    "cold",
    "cough",
    "dew point",
    "dry",
    "flu",
    "heat",
    "hot",
    "humid",
    "migraine",
    "pollen",
    "respiratory",
    "skin",
    "sore throat",
    "uv",
]

COMMERCIAL_TERMS = [
    "apparel",
    "auto",
    "beverage",
    "car",
    "cleaning",
    "construction",
    "food",
    "grill",
    "home",
    "lawn",
    "outdoor",
    "retail",
    "shop",
    "travel",
]

FALLBACK_REGIONS = [
    {
        "region": "Northeast / NYC proxy",
        "location": "10001:US",
        "source": "fallback regional proxy",
    },
    {
        "region": "Southeast / Atlanta proxy",
        "location": "30301:US",
        "source": "fallback regional proxy",
    },
    {
        "region": "West Coast / Los Angeles proxy",
        "location": "90001:US",
        "source": "fallback regional proxy",
    },
    {
        "region": "Southwest / Phoenix proxy",
        "location": "85001:US",
        "source": "fallback regional proxy",
    },
]


def norm(value: Any) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w not in STOPWORDS]
    return " ".join(words)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def request_json(path: str, api_key: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={
            "Authorization": f"apikey {api_key}",
            "Accept": "application/json",
            "User-Agent": "revenue-insight-weatherfx-refresh/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"WeatherFX HTTP {exc.code} for {path}: {body[:500]}") from exc


def read_winmo_locations(path: Path) -> dict[str, list[dict[str, str]]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook["Data"]
    rows = sheet.iter_rows(values_only=True)
    headers = [str(h or "").strip() for h in next(rows)]
    index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        record = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
        company = str(record.get("Company Name") or "").strip()
        zip_code = re.sub(r"\D", "", str(record.get("Zip Code") or ""))[:5]
        if not company or len(zip_code) != 5:
            continue
        key = norm(company)
        city = str(record.get("City") or "").strip()
        state = str(record.get("State") or "").strip()
        location = {
            "region": f"{city}, {state}".strip(", "),
            "location": f"{zip_code}:US",
            "source": "Winmo company ZIP proxy",
        }
        if location not in index[key]:
            index[key].append(location)
    return index


def find_locations(account_name: str, index: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    key = norm(account_name)
    if key in index:
        return index[key][:3]
    key_words = set(key.split())
    best: tuple[int, list[dict[str, str]]] | None = None
    for company_key, locations in index.items():
        words = set(company_key.split())
        if not words:
            continue
        overlap = len(key_words & words)
        threshold = max(1, min(len(key_words), len(words)) - 1)
        if overlap >= threshold and (best is None or overlap > best[0]):
            best = (overlap, locations)
    return best[1][:3] if best else []


def catalog_by_id(catalog: dict[str, Any]) -> dict[int, dict[str, str]]:
    rows: list[dict[str, Any]] = []
    if isinstance(catalog.get("catalog"), list):
        rows = catalog["catalog"]
    elif isinstance(catalog.get("entities"), list):
        rows = catalog["entities"]
    elif isinstance(catalog.get("triggers"), list):
        rows = catalog["triggers"]
    elif isinstance(catalog.get("data"), list):
        rows = catalog["data"]
    else:
        for value in catalog.values():
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))

    out: dict[int, dict[str, str]] = {}
    for row in rows:
        entity_type = str(row.get("entityType") or row.get("type") or "triggers").lower()
        if "trigger" not in entity_type:
            continue
        entity_id = row.get("entityId") or row.get("id")
        try:
            numeric_id = int(entity_id)
        except (TypeError, ValueError):
            continue
        out[numeric_id] = {
            "id": str(numeric_id),
            "name": str(row.get("name") or f"Trigger {numeric_id}"),
            "description": str(row.get("description") or ""),
            "labels": ", ".join(row.get("labels") or []) if isinstance(row.get("labels"), list) else str(row.get("labels") or ""),
        }
    return out


def useful_trigger(trigger: dict[str, str], vertical: str) -> bool:
    text = " ".join([trigger.get("name", ""), trigger.get("description", ""), trigger.get("labels", "")]).lower()
    vertical_text = vertical.lower()
    if "pharma" in vertical_text or "health" in vertical_text:
        return any(term in text for term in HEALTH_TERMS)
    return any(term in text for term in HEALTH_TERMS + COMMERCIAL_TERMS)


def batch(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh WeatherFX trigger overlays for ICP Explorer accounts.")
    parser.add_argument("--accounts-json", required=True, type=Path)
    parser.add_argument("--winmo-xlsx", required=True, type=Path)
    parser.add_argument("--summary-json", type=Path)
    args = parser.parse_args()

    account_id = require_env("WEATHERFX_ACCOUNT_ID")
    segment_id = require_env("WEATHERFX_SEGMENT_ID")
    api_key = require_env("WEATHERFX_API_KEY")

    root = f"/v3/accounts/{urllib.parse.quote(account_id)}/segments/{urllib.parse.quote(segment_id)}"
    catalog = catalog_by_id(request_json(f"{root}/catalog.json?entityType=triggers", api_key))
    if not catalog:
        raise SystemExit("WeatherFX catalog returned no trigger definitions")

    accounts = json.loads(args.accounts_json.read_text())
    winmo_locations = read_winmo_locations(args.winmo_xlsx)

    account_locations: dict[str, list[dict[str, str]]] = {}
    unique_locations: set[str] = set()
    matched = 0
    for account in accounts:
        name = account.get("n") or account.get("name") or ""
        locations = find_locations(name, winmo_locations)
        if locations:
            matched += 1
        else:
            locations = FALLBACK_REGIONS
        account_locations[account["account_sfdc_id"]] = locations
        unique_locations.update(location["location"] for location in locations)

    trigger_results: dict[str, list[int]] = {}
    for group in batch(sorted(unique_locations), 10):
        params = urllib.parse.urlencode({"locations": ",".join(group)})
        response = request_json(f"{root}/triggers.json?{params}", api_key)
        raw = response.get("triggers") or {}
        for location, domains in raw.items():
            if isinstance(domains, dict):
                ids = domains.get("wfx") or []
            else:
                ids = domains or []
            trigger_results[location] = [int(i) for i in ids if str(i).isdigit()]
        time.sleep(0.2)

    updated = 0
    for account in accounts:
        vertical = str(account.get("v") or "")
        rows = []
        for location in account_locations[account["account_sfdc_id"]]:
            ids = trigger_results.get(location["location"], [])
            useful = [catalog[i] for i in ids if i in catalog and useful_trigger(catalog[i], vertical)]
            useful = useful[:8]
            rows.append(
                {
                    **location,
                    "active_trigger_count": len(ids),
                    "useful_trigger_count": len(useful),
                    "top_triggers": useful,
                }
            )
        account["weatherfx_overlay"] = {
            "status": "trigger API refreshed; insight/time-curve pending TWC enablement",
            "location_strategy": "Winmo company ZIP proxy where available; regional proxy fallback where missing",
            "regions": rows,
        }
        updated += 1

    args.accounts_json.write_text(json.dumps(accounts, indent=2) + "\n")
    summary = {
        "accounts_updated": updated,
        "accounts_with_winmo_zip_match": matched,
        "accounts_using_fallback_regions": updated - matched,
        "unique_locations_queried": len(unique_locations),
        "catalog_triggers": len(catalog),
    }
    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
