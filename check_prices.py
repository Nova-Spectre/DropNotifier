# check_prices.py
"""
Price tracker optimized for Apify:
- Uses Apify proxy only for Croma (APIFY_PROXY_* env vars).
- Uses default no-proxy browser for other sites.
- Stealth init script applied for Croma contexts.
- Saves failing HTML to debug_failures/ on extraction failure.
Environment vars expected:
  SUPABASE_URL, SUPABASE_KEY, SLACK_WEBHOOK (optional)
  APIFY_PROXY_PASSWORD (optional)  -> Apify Proxy password (find in Apify Console -> Proxy)
  APIFY_PROXY_HOSTNAME (optional)  -> default: proxy.apify.com
  APIFY_PROXY_PORT (optional)      -> default: 8000
  APIFY_PROXY_GROUPS (optional)    -> e.g. RESIDENTIAL
"""
import asyncio
import os
import re
import random
import traceback
import pathlib
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from supabase import create_client
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Error as PlaywrightError

load_dotenv()

# ---------------------------
# Config / Env
# ---------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK")

APIFY_PROXY_PASSWORD = os.getenv("APIFY_PROXY_PASSWORD")  # proxy password (Apify)
APIFY_PROXY_HOSTNAME = os.getenv("APIFY_PROXY_HOSTNAME", "proxy.apify.com")
APIFY_PROXY_PORT = os.getenv("APIFY_PROXY_PORT", "8000")
APIFY_PROXY_GROUPS = os.getenv("APIFY_PROXY_GROUPS", "").strip()  # e.g. RESIDENTIAL

DEBUG_DIR = pathlib.Path("debug_failures")
DEBUG_DIR.mkdir(exist_ok=True)

# Supabase client (requires SUPABASE_URL and SUPABASE_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------
# Helpers
# ---------------------------
def send_slack(message: str):
    if not SLACK_WEBHOOK:
        print("[SLACK] no webhook configured; skipping Slack post.")
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
    except Exception as e:
        print("Slack send error:", e)


def build_apify_proxy_settings():
    """
    Build Playwright proxy dict for Apify proxy or return None if not configured.
    Playwright expects: {"server": "http://host:port", "username": "...", "password": "..."}
    Username encodes groups as: groups-RESIDENTIAL (if APIFY_PROXY_GROUPS set)
    """
    if not APIFY_PROXY_PASSWORD:
        return None
    username = f"groups-{APIFY_PROXY_GROUPS}" if APIFY_PROXY_GROUPS else "auto"
    server = f"http://{APIFY_PROXY_HOSTNAME}:{APIFY_PROXY_PORT}"
    return {"server": server, "username": username, "password": APIFY_PROXY_PASSWORD}


def sanitize_filename(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z-_.]", "_", s)[:240]


# ---------------------------
# Extraction logic
# ---------------------------
async def add_stealth_shims(context):
    """
    Adds a small stealth script to the context to reduce obvious automation flags.
    (We add only minimal, safe shims.)
    """
    await context.add_init_script(
        """
try {
  Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'], configurable: true });
  Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3], configurable: true });
  try {
    const originalQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => (p && p.name === 'notifications') ? Promise.resolve({ state: Notification.permission }) : originalQuery(p);
  } catch(e){}
} catch(e){}
try {
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
  };
} catch(e){}
"""
    )


async def create_context_for(browser, site: str):
    """
    Create a Playwright context configured for the given site.
    For Croma, we also add stealth shims and some extra permissions.
    """
    real_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.6261.95 Safari/537.36"
    )
    base_opts = dict(
        user_agent=real_ua,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )

    if site == "croma":
        ctx = await browser.new_context(
            **base_opts,
            java_script_enabled=True,
            color_scheme="light",
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            geolocation={"longitude": 72.8777, "latitude": 19.0760},
            permissions=["geolocation"],
        )
        await add_stealth_shims(ctx)
        return ctx

    return await browser.new_context(**base_opts)


def normalize_price_string(price_raw: str):
    if not price_raw:
        return None
    s = str(price_raw)
    s = s.replace("₹", "").replace("INR", "").replace("MRP", "")
    s = re.sub(r"[^\d.,]", "", s)
    if s.count(",") and s.count(".") == 0:
        s = s.replace(",", "")
    s = s.replace(",", "")
    return s.strip() if s else None


async def extract_price(site: str, html: str, page=None):
    """
    Return raw price string (may contain digits/comma/dot) or None.
    For croma we attempt page.evaluate checks first (if page provided), else HTML parse.
    """
    soup = BeautifulSoup(html, "html.parser")
    price_raw = None

    if site == "flipkart":
        el = soup.select_one(".Nx9bqj.CxhGGd")
        price_raw = el.text if el else None

    elif site == "amazon":
        whole = soup.select_one("span.a-price-whole")
        frac = soup.select_one("span.a-price-fraction")
        if whole:
            whole_digits = "".join(filter(str.isdigit, whole.text))
            frac_digits = "".join(filter(str.isdigit, frac.text)) if frac else "00"
            price_raw = f"{whole_digits}.{frac_digits}"

    elif site == "reliance":
        el = soup.select_one("div.product-price")
        if el:
            price_raw = el.get_text(strip=True).replace("MRP", "")

    elif site == "croma":
        # Prefer reading from page JS objects if possible
        if page:
            try:
                found = await page.evaluate(
                    """() => {
                        function inspect(o) {
                          try {
                            if (!o || typeof o !== 'object') return null;
                            if (Object.prototype.hasOwnProperty.call(o, 'sellingPrice')) {
                              let sp = o['sellingPrice'];
                              if (sp && (sp.value || sp.value === 0)) return sp.value;
                            }
                            if (Object.prototype.hasOwnProperty.call(o, 'pdpPriceData')) {
                              let pd = o['pdpPriceData'];
                              if (pd && pd.sellingPrice && (pd.sellingPrice.value || pd.sellingPrice.value === 0)) return pd.sellingPrice.value;
                            }
                            if (o && o.price && o.price.sellingPrice && o.price.sellingPrice.value) return o.price.sellingPrice.value;
                          } catch(e){}
                          return null;
                        }
                        try {
                          const keys = Object.keys(window);
                          for (let i=0;i<keys.length;i++){
                            try {
                              const v = window[keys[i]];
                              if (!v || typeof v !== 'object') continue;
                              let r = inspect(v);
                              if (r) return String(r);
                              const subkeys = Object.keys(v || {});
                              for (let j=0;j<subkeys.length;j++){
                                try {
                                  const vv = v[subkeys[j]];
                                  if (vv && typeof vv === 'object') {
                                    let r2 = inspect(vv);
                                    if (r2) return String(r2);
                                  }
                                } catch(e2){}
                              }
                            } catch(e1){}
                          }
                        } catch(e){}
                        return null;
                    }"""
                )
                if found:
                    price_raw = str(found)
            except Exception:
                price_raw = price_raw

        # fallback: regex/script/DOM in HTML
        if not price_raw:
            regexes = [
                r'"sellingPrice"\s*:\s*{\s*"value"\s*:\s*"?(?P<v>[\d,]+)"?',
                r'"pdpPriceData"\s*:\s*{[^}]*"sellingPrice"\s*:\s*{[^}]*"value"\s*:\s*"?(?P<v>[\d,]+)"?',
                r'"value"\s*:\s*"(?P<v>[\d,]+)"\s*,\s*"currency"',
                r'"mrp"\s*:\s*{\s*"value"\s*:\s*"(?P<v>[\d,]+)"',
            ]
            for rx in regexes:
                m = re.search(rx, html, re.IGNORECASE | re.DOTALL)
                if m:
                    price_raw = m.group("v")
                    break

            if not price_raw:
                for s in soup.find_all("script"):
                    text = s.string or s.get_text() or ""
                    if not text:
                        continue
                    for rx in regexes:
                        m = re.search(rx, text, re.IGNORECASE | re.DOTALL)
                        if m:
                            price_raw = m.group("v")
                            break
                    if price_raw:
                        break

        if not price_raw:
            el = (
                soup.select_one("#pdp-product-price")
                or soup.select_one("div.product-price")
                or soup.select_one("span.pdp-selling-price")
                or soup.select_one("span.price")
                or soup.select_one("span.offer-price")
            )
            if el:
                price_raw = el.get("value") or el.get_text(strip=True)

    return price_raw


# ---------------------------
# Page price extraction flow
# ---------------------------
async def get_price_with_context(browser, site: str, url: str):
    attempt = 0
    last_exc = None
    while attempt < 2:
        attempt += 1
        context = await create_context_for(browser, site)
        page = await context.new_page()
        try:
            # tiny human-like delay
            await asyncio.sleep(random.uniform(0.2, 0.6))
            try:
                await page.mouse.move(random.randint(10, 60), random.randint(10, 60), steps=3)
            except Exception:
                pass

            if site == "croma":
                await page.goto(url, timeout=90000, wait_until="networkidle")
                await page.wait_for_timeout(1200)
            else:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")

            # specific wait for reliance price
            if site == "reliance":
                try:
                    await page.wait_for_selector("div.product-price", timeout=15000)
                except Exception:
                    pass

            html = await page.content()
            price_raw = await extract_price(site, html, page=page)

            if not price_raw:
                # wait a tiny bit and retry the DOM
                await page.wait_for_timeout(1000)
                html2 = await page.content()
                price_raw = await extract_price(site, html2, page=page)

            if not price_raw:
                # save HTML only on failure for debugging
                try:
                    parsed = url.replace("://", "_").replace("/", "_")
                    fname = sanitize_filename(f"{site}_{parsed}") + ".html"
                    file_path = DEBUG_DIR / fname
                    file_path.write_text(html, encoding="utf-8")
                    print(f"[DEBUG] saved failing HTML to {file_path}")
                    snippet = (html[:800] + "...") if len(html) > 800 else html
                    print("[DEBUG] HTML snippet:", snippet)
                except Exception as dump_e:
                    print("[DEBUG] failed writing debug HTML:", dump_e)

                raise Exception("Price not found (no matching pattern / selector).")

            price_str = normalize_price_string(price_raw)
            if not price_str:
                raise Exception("Price normalization failed.")

            await page.close()
            await context.close()
            return float(price_str)

        except PlaywrightError as e:
            msg = str(e)
            # detect proxy/network problems
            if "ERR_PROXY_CONNECTION_FAILED" in msg or "ERR_NETWORK_CHANGED" in msg or "ERR_TUNNEL_CONNECTION_FAILED" in msg:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                raise e  # propagate proxy/network errors up
            last_exc = e
            print(f"[WARN] attempt {attempt} failed for {url}: {e}")
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            await asyncio.sleep(0.7 * attempt)
            continue

        except Exception as e:
            last_exc = e
            print(f"[WARN] attempt {attempt} failed for {url}: {e}")
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            await asyncio.sleep(0.7 * attempt)
            continue

    print(f"[ERROR] get_price_with_context failed for {url}: {last_exc}")
    return None


# ---------------------------
# Main run flow
# ---------------------------
async def run_price_check():
    print("Fetching items from Supabase...")
    resp = supabase.table("tracked_items").select("*").eq("active", True).execute()
    if hasattr(resp, "data"):
        items = resp.data
    elif isinstance(resp, dict) and "data" in resp:
        items = resp["data"]
    else:
        items = resp

    if not items:
        print("No active items found.")
        return

    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-infobars",
    ]

    # apify proxy config (only used for Croma)
    apify_proxy = build_apify_proxy_settings()

    async with async_playwright() as p:
        # helper to start browsers: proxy_browser (if apify_proxy set) and default_browser
        async def _launch_browser(use_proxy: bool):
            kwargs = {"headless": True, "args": launch_args}
            if use_proxy and apify_proxy:
                kwargs["proxy"] = apify_proxy
            return await p.chromium.launch(**kwargs)

        # default browser (no proxy) used for non-Croma and fallback
        try:
            browser_default = await _launch_browser(False)
        except Exception as e:
            print("[ERROR] could not launch default browser:", e)
            return

        browser_proxy = None
        if apify_proxy:
            try:
                browser_proxy = await _launch_browser(True)
            except Exception as e:
                print("[WARN] could not launch proxy browser; continuing with default only:", e)
                browser_proxy = None

        try:
            for item in items:
                site = (item.get("site") or "").lower()
                url = item.get("product_url")
                target_price = item.get("target_price") or 0

                print(f"Checking {site.upper() if site else site} → {url}")

                try:
                    price = None
                    # If site is croma, try proxy browser first if available
                    if site == "croma" and browser_proxy:
                        try:
                            price = await get_price_with_context(browser_proxy, site, url)
                        except PlaywrightError as pe:
                            # Proxy-level error; retry once with default no-proxy browser
                            print("[WARN] proxy connection error for Croma. Retrying without proxy:", pe)
                            price = await get_price_with_context(browser_default, site, url)
                    else:
                        price = await get_price_with_context(browser_default, site, url)

                    if price is None:
                        msg = f"❗ ERROR: Could not extract price.\nSite: {site}\nURL: {url}"
                        print(msg)
                        send_slack(msg)
                        supabase.table("price_history").insert({
                            "tracked_item_id": item["id"],
                            "price": None,
                        }).execute()
                        continue

                    # update tracked_items and history
                    supabase.table("tracked_items").update({
                        "last_price": price,
                        "last_checked_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", item["id"]).execute()

                    supabase.table("price_history").insert({
                        "tracked_item_id": item["id"],
                        "price": price,
                    }).execute()

                    if price >= (target_price or 0):
                        print(f"Price OK: {price}, not below target {target_price}")
                        continue

                    if not item.get("notified"):
                        msg = (
                            f"💰 PRICE DROP ALERT!\n"
                            f"Site: *{site}*\n"
                            f"URL: {url}\n"
                            f"Current Price: ₹{price}\n"
                            f"Target Price: ₹{target_price}"
                        )
                        send_slack(msg)
                        supabase.table("tracked_items").update({
                            "notified": True
                        }).eq("id", item["id"]).execute()

                except Exception as e:
                    error_msg = f"❗ CRITICAL ERROR scraping URL:\n{url}\nError: {e}"
                    print(error_msg)
                    send_slack(error_msg)
                    traceback.print_exc()
        finally:
            try:
                if browser_proxy:
                    await browser_proxy.close()
            except Exception:
                pass
            try:
                if browser_default:
                    await browser_default.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_price_check())
