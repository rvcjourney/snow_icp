import asyncio
import argparse
import json
import os
import random
import string
import sys
from playwright.async_api import async_playwright

SNOV_URL    = "https://app.snov.io"
SNOV_DOMAIN = "app.snov.io"


# ─────────────────────────────────────────────────────────────────────────────
# Cookie helpers
# ─────────────────────────────────────────────────────────────────────────────

def generate_list_name(length: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    return "lst" + "".join(random.choices(chars, k=length))


def _parse_cookie_header(raw: str) -> list[dict]:
    SKIP = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly"}
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name.lower() in SKIP:
            continue
        cookies.append({
            "name":   name,
            "value":  value.strip(),
            "domain": SNOV_DOMAIN,
            "path":   "/",
        })
    return cookies


def _normalize_cookie(c: dict) -> dict:
    """Sanitise a cookie dict so Playwright's add_cookies() accepts it.
    Handles case-insensitive keys from different browser export formats."""
    # Build a lowercase → original-value map so we handle any key casing
    lc = {k.lower(): v for k, v in c.items()}

    # Playwright field name → lowercase source key(s) to look for
    # Covers: standard, Postman ("key"), EditThisCookie, CDP, Firefox exports
    FIELD_MAP = {
        "name":     ["name", "key", "cookiename", "cookie_name"],
        "value":    ["value", "val", "content"],
        "domain":   ["domain"],
        "path":     ["path"],
        "expires":  ["expires", "expirationdate", "expiry", "expire", "max-age"],
        "httpOnly": ["httponly", "http_only", "ishttp"],
        "secure":   ["secure", "issecure"],
    }

    result = {}
    for playwright_key, candidates in FIELD_MAP.items():
        for candidate in candidates:
            if candidate in lc:
                result[playwright_key] = lc[candidate]
                break

    result.setdefault("domain", SNOV_DOMAIN)
    result.setdefault("path",   "/")
    return result


def load_cookies(raw: str) -> list[dict]:
    """
    Accept three formats:
      • JSON array   – [{"name":"…","value":"…"}, …]
      • JSON object  – {"name":"…","value":"…"}
      • Header str   – name=val; name2=val2; …
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("No cookies provided.")

    if raw.startswith("["):
        data: list[dict] = json.loads(raw)
        cookies = [_normalize_cookie(c) for c in data]
    elif raw.startswith("{"):
        cookies = [_normalize_cookie(json.loads(raw))]
    else:
        cookies = _parse_cookie_header(raw)

    # Drop any cookie missing name or value — Playwright rejects them
    valid = [c for c in cookies if c.get("name") and c.get("value") is not None]
    if not valid:
        raise ValueError(
            f"Parsed {len(cookies)} cookie(s) but none had a valid 'name' field. "
            "Check the format — paste as 'name=val; name2=val2' or a JSON array."
        )
    return valid


# ─────────────────────────────────────────────────────────────────────────────
# Core scraper  (called by both CLI and the web UI)
# ─────────────────────────────────────────────────────────────────────────────

async def run_scraper(
    config:   dict,
    log_fn=   print,
    meta_fn=  None,          # meta_fn(key: str, value) for structured events
) -> list[dict]:
    """
    config keys:
      cookies        – list of Playwright cookie dicts (pre-parsed)
      location       – str
      industry       – str
      max_companies  – int
    """
    cookies        = config["cookies"]
    location_query = config.get("location",      "Mumbai")
    industry_query = config.get("industry",      "Fastener")
    max_companies  = config.get("max_companies", 15)
    company_sizes  = config.get("company_size",  [])
    list_name      = generate_list_name()

    if meta_fn:
        meta_fn("list_name", list_name)

    log_fn(f"  Auto-generated list name : {list_name}")
    log_fn(f"  Location                 : {location_query}")
    log_fn(f"  Industry                 : {industry_query}")
    log_fn(f"  Company size             : {', '.join(company_sizes) if company_sizes else 'any'}")
    log_fn(f"  Max companies            : {max_companies}")

    async with async_playwright() as pw:
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "1") != "0"
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        page = await context.new_page()

        # Auto-close any popup/new-tab snov.io opens after our main page (e.g. Web Store promos)
        context.on("page", lambda p: asyncio.ensure_future(p.close()))
        page.set_default_timeout(60_000)   # 60 s for all waits/clicks

        # ── 1. Navigate ───────────────────────────────────────────────────────
        log_fn("▶ [1/12] Navigating to Companies page…")
        await page.goto(f"{SNOV_URL}/companies/all", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        if any(kw in page.url for kw in ("login", "signin", "sign-in")):
            await browser.close()
            raise RuntimeError(
                "Cookie auth failed — redirected to login. Refresh your cookies."
            )
        log_fn("  ✔  Authenticated via cookies.")

        # ── 2. Create list ────────────────────────────────────────────────────
        log_fn(f"▶ [2/12] Creating company list '{list_name}'…")

        new_list_btn = page.locator('[data-test="aside-create-list"]')
        await new_list_btn.wait_for(state="visible", timeout=10_000)
        await new_list_btn.click()

        await page.wait_for_selector('[data-test="create-list-modal"]', timeout=10_000)

        name_input = page.locator('[data-test="create-list-modal"] input.snov-input__input')
        await name_input.wait_for(state="visible", timeout=5_000)
        await name_input.click(click_count=3)
        await name_input.fill(list_name)

        await page.locator('[data-test="create-list-modal"] [data-test="snov-modal-btn-primary"]').click()
        await page.wait_for_timeout(2000)
        log_fn(f"  ✔  List '{list_name}' created.")

        # ── 3. Find companies ─────────────────────────────────────────────────
        log_fn("▶ [3/12] Clicking 'Find companies'…")
        await page.get_by_role("button", name="Find companies").click()
        await page.wait_for_url(
            "**/database-search/companies**", wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(1500)

        companies_tab = page.get_by_role("tab", name="Companies")
        if await companies_tab.is_visible():
            await companies_tab.click()
            await page.wait_for_timeout(1000)

        filter_icon = page.locator(
            "button[aria-label*='filter'], [class*='filter'] button"
        ).first
        if await filter_icon.is_visible():
            await filter_icon.click()
            await page.wait_for_timeout(1000)

        # ── 4. Location filter ────────────────────────────────────────────────
        log_fn(f"▶ [4/12] Setting location: {location_query}")
        location_input = page.get_by_placeholder("Select location")
        await location_input.click()
        await location_input.fill(location_query)
        await page.wait_for_timeout(2000)
        loc_option = page.locator(
            '.snov-filter__option:not(.snov-filter__option-switcher)'
        ).first
        await loc_option.scroll_into_view_if_needed()
        await loc_option.click(force=True)
        await page.wait_for_timeout(1000)
        log_fn("  ✔  Location set.")

        # ── 5. Industry filter ────────────────────────────────────────────────
        log_fn(f"▶ [5/12] Setting industry: {industry_query}")
        industry_input = page.get_by_placeholder("Select industry")
        await industry_input.click()
        await industry_input.fill(industry_query)
        await page.wait_for_timeout(2000)
        ind_option = page.locator(
            '.snov-filter__option:not(.snov-filter__option-switcher)'
        ).first
        await ind_option.scroll_into_view_if_needed()
        await ind_option.click(force=True)
        await page.wait_for_timeout(1000)
        log_fn("  ✔  Industry set.")

        # ── 6. Company size filter ────────────────────────────────────────────
        if company_sizes:
            log_fn(f"▶ [6/12] Setting company size: {', '.join(company_sizes)}")

            # Try to expand the "Company size" section if it's collapsed
            for header_text in ("Company size", "Employees", "Employee count", "Size"):
                try:
                    hdr = page.get_by_text(header_text, exact=False).first
                    if await hdr.is_visible(timeout=2_000):
                        await hdr.scroll_into_view_if_needed()
                        await hdr.click()
                        await page.wait_for_timeout(600)
                        break
                except Exception:
                    pass

            for size in company_sizes:
                clicked = False
                # Try selectors from most to least specific
                candidates = [
                    page.locator('.snov-filter__option').filter(has_text=size),
                    page.locator('[class*="size"] .snov-filter__option').filter(has_text=size),
                    page.get_by_role("option", name=size),
                    page.get_by_role("checkbox", name=size),
                    page.locator(f'label:has-text("{size}")'),
                    page.locator(f'span:has-text("{size}")').filter(
                        has=page.locator('[class*="filter"], [class*="option"], [class*="check"]')
                    ),
                ]
                for loc in candidates:
                    try:
                        el = loc.first
                        if await el.is_visible(timeout=1_500):
                            await el.scroll_into_view_if_needed()
                            await el.click(force=True)
                            await page.wait_for_timeout(400)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    log_fn(f"  ✔  Selected size: {size}")
                else:
                    log_fn(f"  ⚠  Could not find size option: {size} (check size_debug.png)")

            log_fn("  ✔  Company size step done.")
        else:
            log_fn("▶ [6/12] Company size: any (skipped)")

        # ── 7. Search ─────────────────────────────────────────────────────────
        log_fn("▶ [7/12] Running search…")
        await page.get_by_role("button", name="Search").click()
        await page.wait_for_timeout(3000)

        # ── 7. Collect company data ───────────────────────────────────────────
        log_fn("▶ [8/12] Collecting company data from search results…")
        company_data: list[dict] = []

        rows      = page.locator("table tbody tr")
        row_count = min(await rows.count(), max_companies)

        for i in range(row_count):
            row = rows.nth(i)
            try:
                name_el = row.locator("a[href*='/companies/view']").first
                if not await name_el.count():
                    continue
                name = (await name_el.inner_text()).strip()
                if not name:
                    continue

                # Website: external link within the same row
                website = "N/A"
                ext_el = row.locator(
                    "a[href^='http']:not([href*='snov.io']), "
                    "a[href^='https']:not([href*='snov.io'])"
                ).first
                if await ext_el.count():
                    href = await ext_el.get_attribute("href")
                    if href:
                        website = href.strip()

                # Location within the same row
                loc = location_query
                for sel in ("[class*='location']", "[class*='city']", "[class*='country']"):
                    loc_el = row.locator(sel).first
                    if await loc_el.count():
                        t = (await loc_el.inner_text()).strip()
                        if t:
                            loc = t
                            break

                company_data.append({
                    "name":     name,
                    "location": loc,
                    "website":  website,
                })
            except Exception:
                continue

        log_fn(f"  ✔  Found {len(company_data)} companies.")

        # ── 8. Select all ─────────────────────────────────────────────────────
        log_fn("▶ [9/12] Selecting all companies…")
        # The SVG (.snv-checkbox__box) overlays the input and intercepts pointer
        # events. Click the SVG directly — it is the actual click target.
        await page.locator('thead .snv-checkbox__box').first.click()
        await page.wait_for_timeout(1500)

        # ── 9. Add to list ────────────────────────────────────────────────────
        log_fn(f"▶ [10/12] Adding to list '{list_name}'…")
        await page.get_by_text("Add to list").click()
        await page.wait_for_timeout(1000)

        list_option = page.get_by_role("option", name=list_name).first
        if not await list_option.is_visible():
            list_option = page.get_by_text(list_name).first
        await list_option.click()
        await page.wait_for_timeout(3000)
        log_fn(f"  ✔  Companies added to '{list_name}'.")

        # ── 10. Navigate to list ──────────────────────────────────────────────
        log_fn("▶ [11/12] Opening list…")
        await page.goto(f"{SNOV_URL}/companies/all", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.get_by_role("link", name=list_name).first.click()
        await page.wait_for_timeout(2000)
        log_fn(f"  ✔  Opened '{list_name}'.")

        # ── 11. Scrape website links (row-by-row from list view) ─────────────
        log_fn("▶ [12/12] Scraping website links from list…")
        website_map: dict[str, str] = {}

        list_rows = page.locator("table tbody tr")
        for i in range(await list_rows.count()):
            row = list_rows.nth(i)
            try:
                name_el = row.locator("a[href*='/companies/view']").first
                if not await name_el.count():
                    continue
                name = (await name_el.inner_text()).strip()
                if not name:
                    continue

                # External website link within the same row
                ext_el = row.locator(
                    "a[href^='http']:not([href*='snov.io']), "
                    "a[href^='https']:not([href*='snov.io'])"
                ).first
                if await ext_el.count():
                    href = await ext_el.get_attribute("href")
                    website_map[name] = href.strip() if href else "N/A"
                else:
                    website_map[name] = "N/A"
            except Exception:
                continue

        log_fn(f"  ✔  Scraped {len(website_map)} website link(s).")

        final_data: list[dict] = []
        for idx, cd in enumerate(company_data, 1):
            name = cd["name"]
            # Prefer website from list view; fall back to what step 8 found
            website = website_map.get(name) or cd.get("website", "N/A")
            final_data.append({
                "no":       idx,
                "name":     name,
                "location": cd["location"],
                "website":  website,
            })

        log_fn(f"  Total: {len(final_data)} companies collected.")

        await browser.close()
        return final_data


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_multiline(banner: str) -> str:
    print(banner)
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "" and lines:
            break
        lines.append(line)
    return " ".join(lines).strip()


def _ask(prompt: str, default: str) -> str:
    val = input(prompt).strip()
    return val if val else default


def _ask_int(prompt: str, default: int) -> int:
    val = input(prompt).strip()
    return int(val) if val.isdigit() else default


def get_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Snov.io company scraper — cookie-authenticated, dynamic inputs"
    )
    p.add_argument("--location",     help="Location filter  (e.g. Mumbai)")
    p.add_argument("--industry",     help="Industry filter  (e.g. Fastener)")
    p.add_argument("--max",          type=int, default=0, dest="max_companies",
                   help="Max companies (default: 15)")
    p.add_argument("--cookies",      help="Cookie string or JSON array")
    p.add_argument("--cookies-file", dest="cookies_file",
                   help="Path to a file containing cookies")
    return p.parse_args()


async def main() -> list[dict]:
    args = get_args()

    print("\n" + "═" * 62)
    print("  Snov.io Company Scraper  (CLI mode)")
    print("═" * 62)

    if args.cookies:
        raw_cookies = args.cookies
    elif args.cookies_file:
        with open(args.cookies_file, encoding="utf-8") as f:
            raw_cookies = f.read()
    else:
        raw_cookies = _prompt_multiline(
            "\n[1/4] Paste your snov.io cookies, then press Enter on a blank line.\n"
            "      Accepted formats:\n"
            "        • Header string  →  name=val; name2=val2; …\n"
            "        • JSON array     →  [{\"name\":\"…\",\"value\":\"…\"}, …]\n"
        )

    if not raw_cookies:
        print("  ✗  No cookies provided. Exiting.")
        sys.exit(1)

    try:
        cookies = load_cookies(raw_cookies)
    except Exception as exc:
        print(f"  ✗  Cookie parse error: {exc}")
        sys.exit(1)

    print(f"  ✔  Loaded {len(cookies)} cookie(s).")

    location_query = args.location      or _ask("\n[2/4] Location  [Mumbai]:   ", "Mumbai")
    industry_query = args.industry      or _ask("[3/4] Industry  [Fastener]: ", "Fastener")
    max_companies  = args.max_companies or _ask_int("[4/4] Max companies [15]: ", 15)

    config = {
        "cookies":       cookies,
        "location":      location_query,
        "industry":      industry_query,
        "max_companies": max_companies,
    }

    def cli_meta(key: str, value):
        pass  # list_name is already printed inside run_scraper via log_fn

    return await run_scraper(config, print, cli_meta)


if __name__ == "__main__":
    asyncio.run(main())
