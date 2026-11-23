"""
scraper.py - ShelfWatch DAP page se patches + class images nikalta hai (READ-ONLY).

Usage:
  python scraper.py --login                      # browser khulega, ek baar login karo, window band karo
  python scraper.py --url "<dap link>" --out data/run1 --headless

Yeh kabhi checkbox / "Add to Cluster" pe click nahi karta. Sirf scroll karta hai
(aur agar "Load more" button ho to sirf wahi dabata hai).
"""
import argparse, base64, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

PROFILE = Path(__file__).parent / "browser_profile"   # login session yahin save hota hai
LOGIN_HOME = "https://tool.shelfwatch.io/"

# Page ke andar se data nikalne ka JS. Selectors page ke hisaab se guess hain
# (checkbox wale cards = patches, right side ki images = class images).
EXTRACT_JS = r"""
() => {
  const W = window.innerWidth;
  const out = {patches: [], classImgs: [], group: '', total: null};
  const seenCard = new Set();
  document.querySelectorAll('input[type=checkbox]').forEach(cb => {
    let el = cb.parentElement;
    while (el && el !== document.body && !el.querySelector('img')) el = el.parentElement;
    if (!el || seenCard.has(el)) return;
    if (el.querySelectorAll('input[type=checkbox]').length !== 1) return;
    seenCard.add(el);
    const img = el.querySelector('img');
    const src = img.currentSrc || img.src;
    if (!src) return;
    const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const idxLine = lines.find(l => /^\d+$/.test(l));
    const text = lines.filter(l => !/^\d+$/.test(l)).sort((a, b) => b.length - a.length)[0] || '';
    out.patches.push({idx: idxLine ? parseInt(idxLine) : null, text: text, url: src});
  });
  const patchSrc = new Set(out.patches.map(p => p.url));
  const seen = new Set();
  document.querySelectorAll('img').forEach(img => {
    const r = img.getBoundingClientRect();
    const src = img.currentSrc || img.src;
    if (!src || patchSrc.has(src) || seen.has(src)) return;
    if (r.left > W * 0.65 && r.width > 20 && r.height > 20) { seen.add(src); out.classImgs.push({url: src, area: r.width * r.height}); }
  });
  out.classImgs.sort((a, b) => b.area - a.area);
  const cands = [...document.querySelectorAll('div,span,h1,h2,h3,p')]
    .filter(e => /^Group Name\s*:/i.test((e.innerText || '').trim()))
    .sort((a, b) => a.innerText.length - b.innerText.length);
  if (cands.length) out.group = cands[0].innerText.replace(/^Group Name\s*:/i, '').trim().split('\n')[0];
  const m = document.body.innerText.match(/of\s+(\d+)\s+items/i);
  if (m) out.total = parseInt(m[1]);
  out.hasPassword = !!document.querySelector('input[type=password]');
  return out;
}
"""

SCROLL_JS = r"""
() => {
  const cb = document.querySelector('input[type=checkbox]');
  if (!cb) return null;
  let el = cb.parentElement;
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 20 && /(auto|scroll)/.test(s.overflowY)) break;
    el = el.parentElement;
  }
  if (!el || el === document.body) { window.scrollBy(0, innerHeight * 0.9); return 1; }
  el.scrollTop += el.clientHeight * 0.9;
  return 1;
}
"""


def log(*a):
    print(*a, flush=True)


def do_login():
    PROFILE.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_HOME)
        log("Browser khul gaya. ShelfWatch mein login karo, phir browser window band kar do.")
        while True:
            try:
                if not ctx.pages:
                    break
                time.sleep(1)
            except Exception:
                break
    log("Login session save ho gaya.")


def fetch_one(args):
    sess, url, path = args
    try:
        if url.startswith("data:"):
            path.write_bytes(base64.b64decode(url.split(",", 1)[1]))
            return True
        for _ in range(3):
            r = sess.get(url, timeout=30)
            if r.ok and r.content:
                path.write_bytes(r.content)
                return True
            time.sleep(1)
    except Exception:
        pass
    return False


def scrape(url, out_dir, headless=True, max_rounds=600):
    out = Path(out_dir)
    (out / "patches").mkdir(parents=True, exist_ok=True)
    (out / "class_images").mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(exist_ok=True)
    api_log = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Network calls log - debugging ke liye (agar DOM scraping fail ho to is file se API dhoondh sakte hain)
        def on_resp(r):
            try:
                if "json" in (r.headers.get("content-type") or ""):
                    api_log.append({"url": r.url, "status": r.status})
            except Exception:
                pass
        page.on("response", on_resp)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        first = page.evaluate(EXTRACT_JS)
        if first.get("hasPassword") or ("login" in page.url.lower() and not first["patches"]):
            ctx.close()
            log("LOGIN_REQUIRED")
            sys.exit(2)

        collected = {}
        group, class_imgs, total = first["group"], first["classImgs"], first["total"]
        no_progress = 0
        for rnd in range(max_rounds):
            data = page.evaluate(EXTRACT_JS)
            before = len(collected)
            for pt in data["patches"]:
                collected.setdefault(pt["idx"] if pt["idx"] is not None else pt["url"], pt)
            group = group or data["group"]
            total = data["total"] or total
            if data["classImgs"] and not class_imgs:
                class_imgs = data["classImgs"]
            log(f"PROGRESS collected={len(collected)} total={total}")
            if total and len(collected) >= total:
                break
            # "Load more" button ho to dabao (sirf yahi button)
            btn = page.get_by_role("button", name=re.compile(r"load more|show more", re.I))
            if btn.count():
                try:
                    btn.first.click(timeout=2000)
                except Exception:
                    pass
            page.evaluate(SCROLL_JS)
            page.wait_for_timeout(700)
            no_progress = no_progress + 1 if len(collected) == before else 0
            if no_progress >= 8:
                break

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        final_url = page.url
        ctx.close()

    if not collected:
        log("NO_PATCHES")
        (out / "network_log.json").write_text(json.dumps(api_log, indent=1))
        sys.exit(3)

    if total and len(collected) < total:
        log(f"WARNING collected {len(collected)} but page shows {total}")

    patches = sorted(collected.values(), key=lambda x: (x["idx"] is None, x["idx"] or 0))
    sess = requests.Session()
    sess.cookies.update(cookies)
    sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": final_url})

    jobs = []
    for i, pt in enumerate(patches, 1):
        pt["url"] = urljoin(final_url, pt["url"])
        pt["file"] = f"patches/{i:04d}.jpg"
        jobs.append((sess, pt["url"], out / pt["file"]))
    for i, ci in enumerate(class_imgs, 1):
        ci["url"] = urljoin(final_url, ci["url"])
        ci["file"] = f"class_images/{i:02d}.jpg"
        jobs.append((sess, ci["url"], out / ci["file"]))

    log(f"DOWNLOADING {len(jobs)} images")
    with ThreadPoolExecutor(max_workers=8) as ex:
        ok = list(ex.map(fetch_one, jobs))
    failed = sum(1 for x in ok if not x)
    if failed:
        log(f"WARNING {failed} images download nahi hui")

    patches = [p for p in patches if (out / p["file"]).exists()]
    class_imgs = [c for c in class_imgs if (out / c["file"]).exists()]
    result = {"url": url, "group": group or "unknown_group", "total_shown": total,
              "patches": patches, "class_images": class_imgs}
    (out / "data.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    (out / "network_log.json").write_text(json.dumps(api_log, indent=1))
    log("DONE")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--out", default="data/run")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()
    if a.login:
        do_login()
    elif a.url:
        scrape(a.url, a.out, headless=a.headless)
    else:
        ap.print_help()
