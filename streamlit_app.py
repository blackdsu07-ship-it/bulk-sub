import streamlit as st
import asyncio
import random
import pandas as pd
import os
from playwright.async_api import async_playwright

# =========================================
# 1. STREAMLIT CONFIG & PLAYWRIGHT INSTALL
# =========================================
st.set_page_config(page_title="Bulk Newsletter Subscriber", page_icon="🚀", layout="wide")

# This automatically installs Playwright browsers on the Streamlit server
@st.cache_resource
def install_playwright():
    os.system("playwright install chromium")
    os.system("playwright install-deps chromium")

install_playwright()

# =========================================
# 2. BOT CONFIGURATION
# =========================================
MAX_CONCURRENT   = 4
PAGE_LOAD_WAIT   = 3000
DETECT_ATTEMPTS  = 3
POPUP_WAIT       = 8
SUCCESS_WAIT     = 4

# =========================================
# 3. HELPER FUNCTIONS
# =========================================
def parse_domains(text):
    if not text: return []
    raw = text.replace(",", "\n").split("\n")
    domains = []
    for d in raw:
        d = d.strip()
        if not d: continue
        d = d.replace("https://", "").replace("http://", "").split("/")[0]
        if "." in d: domains.append(d)
    return domains

async def remove_overlays(page):
    try:
        await page.evaluate("""
            document.querySelectorAll(
                '.overlay,.modal-backdrop,.cookie-banner,' +
                '.cookie-consent,.gdpr,[id*="cookie" i],[class*="cookie" i],' +
                '[class*="sticky-bar" i],[class*="promo-bar" i],[class*="floating" i]'
            ).forEach(e => e.remove());
            document.body.style.overflow = 'auto';
        """)
    except: pass

async def scroll_to_bottom(page):
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.2)
        await page.evaluate("window.scrollBy(0, -300)")
        await asyncio.sleep(0.5)
    except: pass

async def trigger_popups(page):
    try:
        await page.evaluate("""
            const keywords = ['subscribe','sign up','signup','join','newsletter','offer'];
            document.querySelectorAll('button, a, [role="button"]').forEach(el => {
                const txt = (el.innerText || '').toLowerCase();
                if (keywords.some(k => txt.includes(k))) { try { el.click(); } catch(e) {} }
            });
        """)
        await asyncio.sleep(0.8)
    except: pass

async def find_email_input(page):
    selectors = [
        "input[type='email']", "input[name*='email' i]", "input[id*='email' i]",
        "input[placeholder*='email' i]", "input[autocomplete='email']", ".hs-email"
    ]
    
    async def check(el):
        try:
            if await el.is_visible():
                box = await el.bounding_box()
                if box and box["width"] > 50: return el
        except: return None
        return None

    # Pass 1: Main View
    for sel in selectors:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 3)):
                el = await check(locs.nth(i))
                if el: return el
        except: pass

    # Pass 2: Footer
    await scroll_to_bottom(page)
    await asyncio.sleep(0.8)
    for sel in selectors:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 3)):
                el = await check(locs.nth(i))
                if el: return el
        except: pass

    return None

async def click_submit(page, email_el=None):
    if email_el:
        try:
            done = await page.evaluate("""(el) => {
                const form = el.closest('form');
                if (form) {
                    const btn = form.querySelector('button[type=submit],input[type=submit]');
                    if (btn) { btn.click(); return true; }
                    form.submit(); return true;
                } return false;
            }""", email_el)
            if done: return True
        except: pass

    for sel in ["button[type='submit']", "input[type='submit']", "button:has-text('Subscribe')"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click(force=True, timeout=2000)
                return True
        except: pass

    try:
        await page.keyboard.press("Enter")
        return True
    except: return False

async def check_success(page):
    # STRICTER SUCCESS CHECKING
    success_selectors = [
        ".success-message", ".alert-success", "[data-ui='success-message']", 
        ".klaviyo-form-success", "#success-message"
    ]
    url_signals = ["thank", "success", "confirm", "subscribed", "welcome"]
    success_texts = ["thank you", "subscribed", "check your email", "almost there", "got it"]

    for _ in range(SUCCESS_WAIT):
        await asyncio.sleep(1)
        
        # 1. Check URL change
        try:
            url = page.url.lower()
            if any(sig in url for sig in url_signals): return True
        except: pass

        # 2. Check strict success boxes
        for sel in success_selectors:
            try:
                if await page.locator(sel).count() > 0 and await page.locator(sel).first.is_visible():
                    return True
            except: pass
            
        # 3. Check text content safely
        try:
            combined = "|".join(success_texts)
            if await page.locator(f"text=/{combined}/i").count() > 0: return True
        except: pass

    return False

# =========================================
# 4. CORE PROCESSING LOGIC
# =========================================
async def process_one(context, domain, email, semaphore):
    async with semaphore:
        try:
            page = await context.new_page()
            # Attempt to navigate
            try:
                await page.goto(f"https://{domain}", wait_until="domcontentloaded", timeout=30000)
            except:
                try: await page.goto(f"http://{domain}", wait_until="domcontentloaded", timeout=30000)
                except:
                    await page.close()
                    return {"domain": domain, "status": "Error: Navigation failed"}

            await page.wait_for_timeout(PAGE_LOAD_WAIT)
            await remove_overlays(page)
            await trigger_popups(page)

            # Detect Email Input
            email_input = None
            for _ in range(DETECT_ATTEMPTS):
                email_input = await find_email_input(page)
                if email_input: break
                await asyncio.sleep(1)

            if not email_input:
                await page.close()
                return {"domain": domain, "status": "Failed: No email field found"}

            # FIX: Human-like typing & Form trigger
            try:
                await email_input.scroll_into_view_if_needed()
                await email_input.focus()
                await asyncio.sleep(0.5)
                
                # Type exactly like a human
                await email_input.type(email, delay=random.randint(60, 120))
                await asyncio.sleep(0.5)
                
                # Click away to trigger JS 'blur' (tells React/Vue we finished typing)
                await page.mouse.click(10, 10)
                await asyncio.sleep(0.5)
            except Exception as e:
                await page.close()
                return {"domain": domain, "status": "Error: Could not fill email"}

            # Submit & Verify
            submitted = await click_submit(page, email_input)
            if submitted:
                success = await check_success(page)
                status = "Subscribed ✓" if success else "Submitted (Unconfirmed)"
            else:
                status = "Filled only (No submit button)"

            await page.close()
            return {"domain": domain, "status": status}

        except Exception as e:
            try: await page.close() 
            except: pass
            return {"domain": domain, "status": f"Error: {str(e)[:50]}"}

async def run_bot(domains, email, progress_bar, status_text):
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}", lambda route: route.abort())
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        
        # Process sequentially to update UI in real-time
        for i, domain in enumerate(domains):
            status_text.text(f"Processing ({i+1}/{len(domains)}): {domain}...")
            res = await process_one(context, domain, email, semaphore)
            results.append(res)
            progress_bar.progress((i + 1) / len(domains))

        await browser.close()
    return results

# =========================================
# 5. STREAMLIT USER INTERFACE
# =========================================
st.title("🚀 Bulk Newsletter Subscriber")
st.markdown("Automate email signups. Built with Streamlit & Playwright.")

col1, col2 = st.columns([1, 2])

with col1:
    with st.container(border=True):
        email_addr = st.text_input("🎯 Target Email", placeholder="yourname@example.com")
        domain_list = st.text_area(
            "🌐 Domains (One per line)", 
            placeholder="google.com\napple.com",
            height=250
        )
        start_btn = st.button("Start Automation", type="primary", use_container_width=True)

with col2:
    if start_btn:
        domains = parse_domains(domain_list)
        if not email_addr or not domains:
            st.error("Please enter both an email and at least one valid domain.")
        else:
            st.write("### ⚙️ Live Activity")
            progress_bar = st.progress(0)
            status_text = st.empty()

            # Create a new event loop for Streamlit thread compatibility
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(run_bot(domains, email_addr, progress_bar, status_text))
            
            status_text.success("All tasks completed!")

            # Prepare Data
            df = pd.DataFrame(results)
            total = len(df)
            confirmed = len(df[df["status"] == "Subscribed ✓"])
            unconf = len(df[df["status"] == "Submitted (Unconfirmed)"])
            failed = total - confirmed - unconf

            st.write("### 📊 Results Summary")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total", total)
            m2.metric("✓ Success", confirmed)
            m3.metric("? Unconfirmed", unconf)
            m4.metric("✗ Failed/Error", failed)
            
            st.dataframe(df, use_container_width=True, hide_index=True)
            
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download Results as CSV",
                data=csv,
                file_name='newsletter_results.csv',
                mime='text/csv',
            )
