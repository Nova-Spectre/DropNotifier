import asyncio
import os
import traceback
import requests
from supabase import create_client
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------
# Slack Notify Function
# ------------------------------
def send_slack(message: str):
    try:
        requests.post(SLACK_WEBHOOK, json={"text": message})
    except Exception as e:
        print("Slack error:", e)


# ------------------------------
# Extractors per site
# ------------------------------
async def get_price(playwright, site, url):
    try:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(url, timeout=30000)  # 30 sec timeout
        await page.wait_for_timeout(3000)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        price = None

        # FLIPKART
        if site == "flipkart":
            # Many Flipkart pages use this class
            price_elem = soup.select_one("._30jeq3")
            if price_elem:
                price = price_elem.text

        # AMAZON
        elif site == "amazon":
            price_elem = (
                soup.select_one("#priceblock_ourprice")
                or soup.select_one("#priceblock_dealprice")
                or soup.select_one(".a-price .a-offscreen")
            )
            if price_elem:
                price = price_elem.text

        # CROMA
        elif site == "croma":
            price_elem = soup.select_one(".pdp__price .amount")
            if price_elem:
                price = price_elem.text

        # RELIANCE
        elif site == "reliance":
            price_elem = soup.select_one(".pdp__priceSection .TextWeb__Text-sc")
            if price_elem:
                price = price_elem.text

        await browser.close()

        if not price:
            raise Exception("Price selector not found")

        # Convert price like ₹44,999 → 44999
        clean = ''.join(filter(str.isdigit, price))
        return int(clean)

    except Exception as e:
        return None


# ------------------------------
# Main Execution Logic
# ------------------------------
async def run_price_check():
    print("Fetching items from Supabase...")
    data = supabase.table("tracked_items").select("*").eq("active", True).execute()

    if not data.data:
        print("No active items found.")
        return

    async with async_playwright() as playwright:

        for item in data.data:
            site = item["site"]
            url = item["product_url"]
            target_price = item["target_price"]

            print(f"Checking {site.upper()} → {url}")

            try:
                price = await get_price(playwright, site, url)

                # Handle scraping failures
                if price is None:
                    msg = f"❗ ERROR: Could not extract price.\nSite: {site}\nURL: {url}"
                    send_slack(msg)

                    supabase.table("price_history").insert({
                        "tracked_item_id": item["id"],
                        "price": None,
                    }).execute()
                    continue

                # Update last price
                supabase.table("tracked_items").update({
                    "last_price": price,
                    "last_checked_at": datetime.utcnow().isoformat()
                }).eq("id", item["id"]).execute()

                # Log history
                supabase.table("price_history").insert({
                    "tracked_item_id": item["id"],
                    "price": price,
                }).execute()

                # Success but not target
                if price > target_price:
                    print(f"Price OK: {price}, not below target")
                    continue

                # Price meets target → Slack notification
                if not item["notified"]:
                    msg = (
                        f"💰 PRICE DROP ALERT!\n"
                        f"Site: *{site}*\n"
                        f"URL: {url}\n"
                        f"Current Price: ₹{price}\n"
                        f"Target Price: ₹{target_price}"
                    )
                    send_slack(msg)

                    # Mark as notified
                    supabase.table("tracked_items").update({
                        "notified": True
                    }).eq("id", item["id"]).execute()

            except Exception as e:
                error_msg = f"❗ CRITICAL ERROR scraping URL:\n{url}\nError: {e}"
                print(error_msg)
                send_slack(error_msg)
                traceback.print_exc()


# ------------------------------
# ENTRY
# ------------------------------
if __name__ == "__main__":
    asyncio.run(run_price_check())
