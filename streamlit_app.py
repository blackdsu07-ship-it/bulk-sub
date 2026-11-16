import streamlit as st
import asyncio
import base64
import io
import os
import random
import re
import zipfile
from urllib.parse import unquote_plus, urlparse

import pandas as pd
from playwright.async_api import async_playwright

# =========================================
# 1. STREAMLIT CONFIG & PLAYWRIGHT INSTALL
# =========================================
st.set_page_config(page_title="Bulk Newsletter Subscriber", page_icon="🚀", layout="wide")


@st.cache_resource
def install_playwright():
    os.system("playwright install chromium")
    os.system("playwright install-deps chromium")


install_playwright()

# =========================================
# 2. CONFIG
# =========================================
PAGE_LOAD_WAIT = 1500   # ms after domcontentloaded
REQUEST_WAIT   = 8      # s to wait for the signup request after clicking
SETTLE_WAIT    = 1.5    # s extra after first matching request (let responses finish)

# Requests to these hosts never count as "signup sent"
ANALYTICS = re.compile(
    r"google-analytics|googletagmanager|doubleclick|facebook\.|hotjar|clarity\.ms|"
    r"mixpanel|sentry|newrelic|datadoghq|pinterest|tiktok|snapchat|criteo|taboola|"
    r"outbrain|bing\.com|linkedin\.com/px|segment\.(io|com)|fullstory|hubspot.*analytics",
    re.I,
)
SKIP_TYPES = {"image", "stylesheet", "font", "media", "ping"}

# =========================================
# 3. PAGE JS
# =========================================
CLEAN_OVERLAYS_JS = """
() => {
  const sel = '#onetrust-banner-sdk,#onetrust-consent-sdk,.cookie-banner,.cookie-consent,' +
              '.cc-window,#cookie-banner,#CybotCookiebotDialog,.truste_overlay';
  document.querySelectorAll(sel).forEach(e => {
    if (!e.querySelector('input[type=email]')) e.remove();
  });
  document.body.style.overflow = 'auto';
}
"""

# Only click things that cannot navigate away
TRIGGER_POPUPS_JS = """
() => {
  const kw = ['subscribe', 'sign up', 'signup', 'newsletter'];
  document.querySelectorAll(
    'button,[role="button"],a[href="#"],a[href^="javascript"],a:not([href])'
  ).forEach(el => {
    if (el.closest('form') && el.closest('form').querySelector('input[type=password]')) return;
    const t = (el.innerText || '').toLowerCase();
    if (kw.some(k => t.includes(k))) { try { el.click(); } catch (e) {} }
  });
}
"""

# Score every email-ish input, tag the best newsletter-looking one
TAG_BEST_INPUT_JS = """
() => {
  document.querySelectorAll('[data-bsub-target]').forEach(e => e.removeAttribute('data-bsub-target'));
  const hardEmail = /e-?mail/i;
  const softEmail = /\\bnl\\b|newsletter|subscribe|sign-?up/i;
  const good = /newsletter|subscribe|subscription|signup|sign-up|join|updates|mailing|email list|offers|deals|klaviyo|mailchimp|mc-embedded/i;
  const bad  = /search|login|log-in|signin|sign-in|password|checkout|coupon|contact|comment|account|register/i;
  const sel = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])" +
              ":not([type=button]):not([type=password]):not([type=search]):not([type=file]):not([type=number])";
  let best = null, bestScore = -1e9;
  document.querySelectorAll(sel).forEach(el => {
    if (el.disabled || el.readOnly) return;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (r.width < 50 || r.height < 10 || cs.visibility === 'hidden' || cs.display === 'none') return;
    const form = el.closest('form');
    const wrap = el.closest('label,li,div') || el.parentElement;
    const labels = [...(el.labels || [])].map(l => l.innerText).join(' ');
    const attrs = [el.type, el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                   el.autocomplete, el.inputMode, el.title, labels].join(' ');
    const near = ((wrap && wrap.innerText) || '').slice(0, 80);   // floating-label text
    const isEmail = el.type === 'email' || hardEmail.test(attrs) || hardEmail.test(near) || softEmail.test(attrs);
    if (!isEmail) return;
    const formAttrs = form ? [form.id, String(form.className || ''), form.getAttribute('action') || ''].join(' ') : '';
    const root = form || (wrap && wrap.parentElement) || el.parentElement;
    const ctx = formAttrs + ' ' + attrs + ' ' + ((root && root.innerText) || '').slice(0, 300);
    let score = 0;
    if (el.type === 'email') score += 3;
    if (good.test(ctx)) score += 5;
    if (bad.test(formAttrs + ' ' + attrs)) score -= 8;
    if (form && form.querySelector('input[type=password]')) score -= 12;
    if (form && (form.getAttribute('role') === 'search' || form.querySelector('input[type=search]'))) score -= 12;
    if (el.closest('header,nav,[role=search]')) score -= 6;
    if (el.closest('footer')) score += 2;
    if (el.closest('[class*=popup i],[class*=modal i],[id*=klaviyo i]')) score += 2;
    if (score > bestScore) { bestScore = score; best = el; }
  });
  if (best && bestScore > -8) { best.setAttribute('data-bsub-target', '1'); return true; }
  return false;
}
"""

DUMP_INPUTS_JS = """
() => [...document.querySelectorAll('input:not([type=hidden])')]
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .slice(0, 8)
  .map(e => [e.type, e.name, e.id, e.placeholder, e.getAttribute('aria-label')].join('|'))
  .join(' ; ')
"""

# Tick required consent boxes, fill required text fields, report validity
PREP_FORM_JS = """
(el) => {
  const form = el.closest('form');
  const root = form || el.parentElement.closest('div,section,aside,footer') || document.body;
  root.querySelectorAll("input[type=checkbox][required]").forEach(c => { if (!c.checked) c.click(); });
  root.querySelectorAll("input[required]").forEach(i => {
    const t = (i.type || 'text').toLowerCase();
    if (t === 'text' && !i.value) {
      i.value = 'Alex';
      i.dispatchEvent(new Event('input', {bubbles: true}));
      i.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
  let invalid = null;
  if (form && !form.checkValidity()) {
    const bad = form.querySelector(':invalid');
    invalid = bad ? (bad.name || bad.id || bad.type || 'field') : 'field';
  }
  return {invalid};
}
"""

FIND_BUTTON_JS = """
(el) => {
  const form = el.closest('form');
  const root = form || el.parentElement.closest('div,section,aside,footer') || document.body;
  return root.querySelector("button[type=submit],input[type=submit]")
      || [...root.querySelectorAll("button,[role=button],input[type=button],a")]
           .find(b => /\\b(subscribe|sign up|signup|join|submit|send|notify|get|go)\\b/i
                      .test((b.innerText || b.value || '').trim()))
      || null;
}
"""


# =========================================
# 4. HELPERS
# =========================================
def parse_domains(text):
    if not text:
        return []
    raw = text.replace(",", "\n").split("\n")
    domains = []
    for d in raw:
        d = d.strip()
        if not d:
            continue
        d = d.replace("https://", "").replace("http://", "").split("/")[0]
        if "." in d:
            domains.append(d)
    return domains


def parse_proxy(url):
    if not url or not url.strip():
        return None
    u = urlparse(url.strip() if "://" in url else "http://" + url.strip())
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"}
    if u.username:
        cfg["username"] = unquote_plus(u.username)
        cfg["password"] = unquote_plus(u.password or "")
    return cfg


def request_has_email(req, email):
    """True if this request looks like it carries the email to some backend."""
    try:
        if ANALYTICS.search(req.url) or req.resource_type in SKIP_TYPES:
            return False
        raw = req.url
        try:
            raw += " " + (req.post_data or "")
        except Exception:
            pass
        if email.lower() in unquote_plus(raw).lower():
            return True
        if base64.b64encode(email.encode()).decode().rstrip("=") in raw:
            return True
    except Exception:
        pass
    return False


async def tag_in_any_frame(page):
    """Run the scorer in the main page and every iframe; return a locator for the winner."""
    for frame in page.frames:
        try:
            if await frame.evaluate(TAG_BEST_INPUT_JS):
                return frame.locator("[data-bsub-target='1']").first
        except Exception:
            pass
    return None


async def locate_email_input(page):
    """As-is, then after popups, then after scrolling (lazy footers) twice."""
    loc = await tag_in_any_frame(page)
    if loc:
        return loc
    try:
        await page.evaluate(TRIGGER_POPUPS_JS)
    except Exception:
        pass
    await page.wait_for_timeout(1200)
    loc = await tag_in_any_frame(page)
    if loc:
        return loc
    for _ in range(2):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        loc = await tag_in_any_frame(page)
        if loc:
            return loc
    return None


async def summarize_requests(captured):
    out = []
    for r in captured[:6]:
        try:
            resp = await asyncio.wait_for(r.response(), 8)
            code = resp.status if resp else None
        except Exception:
            code = None
        out.append((r.method, urlparse(r.url).netloc, code))
    return out


async def has_captcha(page):
    try:
        return any(
            re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare", f.url, re.I)
            for f in page.frames
        )
    except Exception:
        return False


# =========================================
# 5. CORE
# =========================================
async def process_one(context, domain, email, semaphore, want_shots):
    async with semaphore:
        res = {"domain": domain, "result": "✗ error", "detail": "", "final_url": ""}
        shot = None
        page = None
        on_req = None
        try:
            page = await context.new_page()

            nav_ok = False
            nav_status = None
            for scheme in ("https", "http"):
                try:
                    resp = await page.goto(f"{scheme}://{domain}", wait_until="domcontentloaded", timeout=20000)
                    nav_status = resp.status if resp else None
                    nav_ok = True
                    break
                except Exception:
                    pass
            if not nav_ok:
                res["detail"] = "navigation failed"
                return res

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(PAGE_LOAD_WAIT)
            try:
                await page.evaluate(CLEAN_OVERLAYS_JS)
            except Exception:
                pass

            loc = await locate_email_input(page)
            if loc is None:
                try:
                    dump = await page.evaluate(DUMP_INPUTS_JS)
                except Exception:
                    dump = ""
                try:
                    info = await page.evaluate(
                        "() => (document.title || '') + ' || ' + "
                        "(document.body ? document.body.innerText.replace(/\\s+/g, ' ').slice(0, 120) : 'NO BODY')"
                    )
                except Exception:
                    info = ""
                res.update(
                    result="✗ no email field",
                    detail=(f"http {nav_status} | iframes: {len(page.frames) - 1} | "
                            f"inputs: {dump or 'none'} | page: {info}")[:400],
                )
                try:
                    res["final_url"] = page.url
                except Exception:
                    pass
                if want_shots:
                    try: shot = await page.screenshot(type="jpeg", quality=60)
                    except Exception: pass
                return res

            # ---- fill ----
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
                await loc.focus()
                await loc.fill("")
                await loc.press_sequentially(email, delay=random.randint(20, 60))
                await loc.evaluate("e => { e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")
            except Exception as e:
                res.update(result="✗ error", detail=f"could not fill email: {str(e)[:60]}")
                return res

            # ---- validity / consents ----
            try:
                prep = await loc.evaluate(PREP_FORM_JS)
            except Exception:
                prep = {"invalid": None}
            if prep.get("invalid"):
                res.update(result="✗ blocked by validation", detail=f"invalid field: {prep['invalid']}")
                if want_shots:
                    try: shot = await page.screenshot(type="jpeg", quality=60)
                    except Exception: pass
                return res

            # ---- submit while recording requests that carry the email ----
            captured = []
            on_req = lambda r: captured.append(r) if request_has_email(r, email) else None
            page.on("request", on_req)

            btn = None
            try:
                btn = (await loc.evaluate_handle(FIND_BUTTON_JS)).as_element()
            except Exception:
                pass

            try:
                if btn:
                    try:
                        await btn.click(timeout=3000)
                    except Exception:
                        await btn.click(timeout=2000, force=True)
                else:
                    sent = await loc.evaluate(
                        "e => { const f = e.closest('form'); "
                        "if (f && f.requestSubmit) { f.requestSubmit(); return true; } return false; }"
                    )
                    if not sent:
                        await loc.press("Enter")
            except Exception as e:
                res.update(result="✗ error", detail=f"submit click failed: {str(e)[:60]}")
                return res

            # ---- wait for the real request ----
            waited = 0.0
            while waited < REQUEST_WAIT and not captured:
                await asyncio.sleep(0.5)
                waited += 0.5
            if captured:
                await asyncio.sleep(SETTLE_WAIT)

            reqs = await summarize_requests(captured)
            try:
                res["final_url"] = page.url
            except Exception:
                pass

            ok = [x for x in reqs if x[2] and x[2] < 400]
            if ok:
                m, host, code = ok[0]
                res.update(result="✓ request accepted", detail=f"{m} {host} → {code}")
            elif reqs:
                m, host, code = reqs[0]
                if code is None:
                    res.update(result="? request sent, no response", detail=f"{m} {host}")
                else:
                    res.update(result="✗ request rejected", detail=f"{m} {host} → {code}")
            else:
                res.update(result="✗ no request sent", detail="click happened, nothing carried the email")

            if not ok and await has_captcha(page):
                res["detail"] += " [captcha present]"

            if want_shots:
                try: shot = await page.screenshot(type="jpeg", quality=60)
                except Exception: pass
            return res

        except Exception as e:
            res.update(result="✗ error", detail=str(e)[:80])
            return res
        finally:
            res["_shot"] = shot
            if page:
                if on_req:
                    try: page.remove_listener("request", on_req)
                    except Exception: pass
                try: await page.close()
                except Exception: pass


async def run_bot(domains, email, concurrency, want_shots, progress_bar, status_text, proxy=None):
    results = []
    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
        try:
            # full Chromium in new headless mode is much harder to fingerprint than the headless shell
            browser = await p.chromium.launch(channel="chromium", **launch_kwargs)
        except Exception:
            browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        # Images are allowed now (captcha widgets and some forms need them); only block heavy media/fonts
        await context.route("**/*.{woff,woff2,ttf,mp4,webm}", lambda route: route.abort())
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        semaphore = asyncio.Semaphore(concurrency)
        tasks = [process_one(context, d, email, semaphore, want_shots) for d in domains]

        done = 0
        for fut in asyncio.as_completed(tasks):
            r = await fut
            results.append(r)
            done += 1
            status_text.text(f"Processed ({done}/{len(domains)}): {r['domain']} — {r['result']}")
            progress_bar.progress(done / len(domains))

        await browser.close()
    return results


# =========================================
# 6. UI
# =========================================
st.title("🚀 Bulk Newsletter Subscriber")
st.markdown(
    "Success = a request carrying your email was sent and the server answered 2xx/3xx. "
    "It does **not** prove a mail will arrive (double opt-in, suppression, dedupe still apply)."
)

col1, col2 = st.columns([1, 2])

with col1:
    with st.container(border=True):
        email_addr = st.text_input("🎯 Target Email", placeholder="yourname@example.com")
        domain_list = st.text_area("🌐 Domains (One per line)", placeholder="google.com\napple.com", height=250)
        concurrency = st.slider("Parallel pages", 1, 8, 3)
        want_shots = st.checkbox("Save screenshot per domain", value=True)
        proxy_url = st.text_input("Proxy (optional)", placeholder="http://user:pass@host:port")
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

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(
                run_bot(domains, email_addr.strip(), concurrency, want_shots, progress_bar, status_text,
                        parse_proxy(proxy_url))
            )
            status_text.success("All tasks completed!")

            # Persist so the download buttons (which rerun the script) don't wipe the results
            shots = {r["domain"]: r.pop("_shot", None) for r in results}
            st.session_state["df"] = pd.DataFrame(results)
            st.session_state["shots"] = {k: v for k, v in shots.items() if v}

    if "df" in st.session_state:
        df = st.session_state["df"]
        total = len(df)
        ok = int(df["result"].str.startswith("✓").sum())
        unk = int(df["result"].str.startswith("?").sum())
        bad = total - ok - unk

        st.write("### 📊 Results Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total", total)
        m2.metric("✓ Request accepted", ok)
        m3.metric("? No response", unk)
        m4.metric("✗ Failed", bad)

        st.dataframe(df, use_container_width=True, hide_index=True)

        st.download_button(
            "📥 Download Results as CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="newsletter_results.csv",
            mime="text/csv",
        )

        shots = st.session_state.get("shots", {})
        if shots:
            buf = io.BytesIO()
            res_by_domain = dict(zip(df["domain"], df["result"]))
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for dom, img in shots.items():
                    tag = re.sub(r"[^a-z0-9]+", "_", res_by_domain.get(dom, "").lower()).strip("_")
                    z.writestr(f"{dom}__{tag}.jpg", img)
            st.download_button(
                "🖼️ Download Screenshots (ZIP)",
                data=buf.getvalue(),
                file_name="screenshots.zip",
                mime="application/zip",
            )
