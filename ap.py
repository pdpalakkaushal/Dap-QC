"""
DAP QC Tool (Streamlit)
Run:  streamlit run app.py
"""
import io, json, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image

HERE = Path(__file__).parent
SCRAPER = HERE / "scraper.py"
DATA = HERE / "data"

st.set_page_config(page_title="DAP QC Tool", page_icon="🔍", layout="wide")


# ---------------------------------------------------------------- QC helpers
@st.cache_resource(show_spinner="CLIP model load ho raha hai (pehli baar thoda time lagega)...")
def load_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("clip-ViT-B-32")


def open_rgb(path):
    return Image.open(path).convert("RGB")


def blur_score(img):
    """Variance of Laplacian, image ko fixed height 256 pe lake."""
    g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    new_w = max(1, int(w * 256 / h))
    g = cv2.resize(g, (new_w, 256))
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def embed(model, imgs, batch=32):
    vecs = []
    for i in range(0, len(imgs), batch):
        v = model.encode(imgs[i:i + batch], convert_to_numpy=True, normalize_embeddings=True)
        vecs.append(v)
    return np.vstack(vecs)


def fuzzy(a, b):
    from difflib import SequenceMatcher
    return round(SequenceMatcher(None, a.lower(), b.lower()).ratio(), 2)


def run_scraper(url, out_dir, headless, status):
    cmd = [sys.executable, str(SCRAPER), "--url", url, "--out", str(out_dir)]
    if headless:
        cmd.append("--headless")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last = ""
    for line in proc.stdout:
        line = line.strip()
        last = line or last
        if line.startswith("PROGRESS"):
            status.write(line.replace("PROGRESS", "Patches collect hue:"))
    proc.wait()
    if proc.returncode == 2:
        raise RuntimeError("Login expire / login nahi hai. Sidebar se 'Login browser kholo' karke login karo.")
    if proc.returncode == 3:
        raise RuntimeError("Koi patch nahi mila. Link check karo ya data/<run>/network_log.json dekho (selectors badalne pad sakte hain).")
    if proc.returncode != 0:
        raise RuntimeError(f"Scraper fail hua: {last}")


def run_qc(data, out_dir, blur_thr, sim_thr, review_thr, min_px, progress):
    model = None
    patches, class_files = data["patches"], data["class_images"]
    imgs = []
    rows = []
    for i, p in enumerate(patches):
        im = open_rgb(out_dir / p["file"])
        imgs.append(im)
        w, h = im.size
        score = blur_score(im)
        rows.append({"Index": p["idx"] if p["idx"] is not None else i + 1, "Product Text": p["text"],
                     "Width": w, "Height": h, "Blur Score": round(score, 1), "URL": p["url"],
                     "_file": p["file"]})
        progress.progress(0.3 * (i + 1) / len(patches), text=f"Blur check {i+1}/{len(patches)}")

    df = pd.DataFrame(rows)
    df["Similarity"] = np.nan
    df["Best Class Img"] = -1
    if class_files:
        model = load_model()
        cimgs = [open_rgb(out_dir / c["file"]) for c in class_files]
        cvec = embed(model, cimgs)
        pvec = []
        for i in range(0, len(imgs), 32):
            pvec.append(embed(model, imgs[i:i + 32]))
            progress.progress(0.3 + 0.6 * min(1, (i + 32) / len(imgs)), text="Class image se match ho raha hai...")
        pvec = np.vstack(pvec)
        sims = pvec @ cvec.T
        df["Similarity"] = sims.max(axis=1).round(3)
        df["Best Class Img"] = sims.argmax(axis=1)
    group = data["group"]
    df["Name Match"] = [fuzzy(t, group) if t else np.nan for t in df["Product Text"]]

    flags = []
    for _, r in df.iterrows():
        f = []
        if r["Width"] < min_px or r["Height"] < min_px:
            f.append("LOW_RES")
        if r["Blur Score"] < blur_thr:
            f.append("BLUR")
        if not np.isnan(r["Similarity"]):
            if r["Similarity"] < sim_thr:
                f.append("MISMATCH")
            elif r["Similarity"] < review_thr:
                f.append("REVIEW")
        flags.append(f)
    df["Status"] = [", ".join(f) if f else "OK" for f in flags]
    return df, imgs


# ---------------------------------------------------------------- Excel
def thumb_bytes(img, max_h=85, max_w=120):
    t = img.copy()
    t.thumbnail((max_w, max_h))
    b = io.BytesIO()
    t.save(b, format="PNG")
    b.seek(0)
    return b


def build_excel(data, df, imgs, out_dir, thr, url):
    buf = io.BytesIO()
    cimgs = [open_rgb(out_dir / c["file"]) for c in data["class_images"]]
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        wb = xw.book
        hdr = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white", "border": 1, "valign": "vcenter"})
        wrap = wb.add_format({"valign": "vcenter", "text_wrap": True})
        center = wb.add_format({"valign": "vcenter", "align": "center"})

        flag_has = lambda k: df["Status"].str.contains(k)
        n = {"total": len(df), "blur": int(flag_has("BLUR").sum()), "lowres": int(flag_has("LOW_RES").sum()),
             "mismatch": int(flag_has("MISMATCH").sum()), "review": int(flag_has("REVIEW").sum()),
             "ok": int((df["Status"] == "OK").sum())}

        # 1) Summary
        summ = pd.DataFrame({"Item": ["Group Name", "DAP Link", "Total patches (collected)", "Total shown on page",
                                      "Blur patches", "Low-res patches", "Mismatch patches", "Review (borderline)",
                                      "OK patches", "Blur threshold", "Mismatch similarity < ", "Review similarity < ",
                                      "Min size (px)", "Run date"],
                             "Value": [data["group"], url, n["total"], data.get("total_shown"), n["blur"], n["lowres"],
                                       n["mismatch"], n["review"], n["ok"], thr["blur"], thr["sim"], thr["review"],
                                       thr["min_px"], datetime.now().strftime("%Y-%m-%d %H:%M")]})
        summ.to_excel(xw, sheet_name="Summary", index=False)
        ws = xw.sheets["Summary"]
        ws.set_column(0, 0, 30)
        ws.set_column(1, 1, 90)

        # image sheets
        def image_sheet(name, sub, with_class):
            ws = wb.add_worksheet(name)
            xw.sheets[name] = ws
            cols = ["Index", "Patch", "Blur Score", "Product Text", "URL"]
            if with_class:
                cols = ["Index", "Patch", "Class Image", "Similarity", "Product Text", "URL"]
            for c, h in enumerate(cols):
                ws.write(0, c, h, hdr)
            ws.set_column(0, 0, 8)
            ws.set_column(1, 2 if with_class else 1, 20)
            ws.set_column(3 if with_class else 2, 3 if with_class else 2, 12)
            ws.set_column(4 if with_class else 3, 4 if with_class else 3, 45)
            ws.set_column(5 if with_class else 4, 5 if with_class else 4, 60)
            for r, (i, row) in enumerate(sub.iterrows(), start=1):
                ws.set_row(r, 70)
                ws.write(r, 0, row["Index"], center)
                ws.insert_image(r, 1, "p.png", {"image_data": thumb_bytes(imgs[i]), "x_offset": 3, "y_offset": 3})
                if with_class:
                    if cimgs:
                        ws.insert_image(r, 2, "c.png", {"image_data": thumb_bytes(cimgs[int(row["Best Class Img"])]),
                                                        "x_offset": 3, "y_offset": 3})
                    ws.write(r, 3, row["Similarity"], center)
                    ws.write(r, 4, row["Product Text"], wrap)
                    ws.write(r, 5, row["URL"], wrap)
                else:
                    ws.write(r, 2, row["Blur Score"], center)
                    ws.write(r, 3, row["Product Text"], wrap)
                    ws.write(r, 4, row["URL"], wrap)
            ws.freeze_panes(1, 0)

        image_sheet("Blur_Patches", df[flag_has("BLUR|LOW_RES")], False)
        image_sheet("Mismatch_Patches", df[flag_has("MISMATCH")].sort_values("Similarity"), True)
        image_sheet("Review_Patches", df[flag_has("REVIEW")].sort_values("Similarity"), True)

        # All patches
        cols = ["Index", "Status", "Blur Score", "Similarity", "Name Match", "Width", "Height", "Product Text", "URL"]
        df[cols].to_excel(xw, sheet_name="All_Patches", index=False)
        ws = xw.sheets["All_Patches"]
        ws.autofilter(0, 0, len(df), len(cols) - 1)
        ws.freeze_panes(1, 0)
        ws.set_column(0, 6, 12)
        ws.set_column(7, 7, 45)
        ws.set_column(8, 8, 60)
        rng = f"A2:I{len(df)+1}"
        for key, color in (("MISMATCH", "#F8A5A5"), ("BLUR", "#FFCC80"), ("LOW_RES", "#FFCC80"), ("REVIEW", "#FFF59D")):
            ws.conditional_format(rng, {"type": "formula", "criteria": f'=ISNUMBER(SEARCH("{key}",$B2))',
                                        "format": wb.add_format({"bg_color": color})})
    return buf.getvalue()


# ---------------------------------------------------------------- UI
st.title("🔍 DAP QC Tool")
st.caption("ShelfWatch DAP link paste karo → blur aur class-image se alag patches ki Excel list milegi. (Read-only: DAP mein kuch change nahi hota)")

with st.sidebar:
    st.header("Settings")
    blur_thr = st.slider("Blur threshold (isse kam = BLUR)", 10, 300, 60)
    sim_thr = st.slider("Mismatch similarity (isse kam = MISMATCH)", 0.50, 0.99, 0.80, 0.01)
    review_thr = st.slider("Review band upper limit", 0.50, 1.00, 0.88, 0.01)
    min_px = st.slider("Min patch size (px) - isse chhota = LOW_RES", 10, 120, 40)
    headless = st.checkbox("Browser chhupa ke chalao (headless)", True)
    st.divider()
    st.subheader("Pehli baar login")
    if st.button("Login browser kholo"):
        subprocess.Popen([sys.executable, str(SCRAPER), "--login"])
        st.info("Browser khulega → ShelfWatch mein login karo → phir browser window band kar do. Ek baar karna kaafi hai.")

links = st.text_area("DAP links (ek line mein ek link)", height=120,
                     placeholder="https://tool.shelfwatch.io/streetsnap/dap?...")

if "results" not in st.session_state:
    st.session_state.results = []

if st.button("▶ Run QC", type="primary"):
    urls = [u.strip() for u in links.splitlines() if u.strip()]
    if not urls:
        st.error("Pehle koi DAP link daalo.")
    st.session_state.results = []
    for k, url in enumerate(urls, 1):
        if not url.startswith("http"):
            st.error(f"Link galat hai: {url}")
            continue
        st.subheader(f"Link {k}")
        out_dir = DATA / f"run_{datetime.now():%Y%m%d_%H%M%S}_{k}"
        prog = st.progress(0.0, text="Shuru ho raha hai...")
        try:
            with st.status("Page se patches nikal raha hoon...", expanded=True) as status:
                run_scraper(url, out_dir, headless, status)
                status.update(label="Patches download ho gaye", state="complete")
            data = json.loads((out_dir / "data.json").read_text())
            if data.get("total_shown") and len(data["patches"]) < data["total_shown"]:
                st.warning(f"Page pe {data['total_shown']} patches dikh rahe the, par {len(data['patches'])} hi mile.")
            if not data["class_images"]:
                st.warning("Class image nahi mili - sirf blur check hoga.")
            df, imgs = run_qc(data, out_dir, blur_thr, sim_thr, review_thr, min_px, prog)
            prog.progress(0.95, text="Excel ban raha hai...")
            xl = build_excel(data, df, imgs, out_dir, {"blur": blur_thr, "sim": sim_thr, "review": review_thr, "min_px": min_px}, url)
            prog.progress(1.0, text="Ho gaya ✅")
            st.session_state.results.append({"group": data["group"], "df": df, "xlsx": xl, "dir": str(out_dir),
                                             "files": [p["file"] for p in data["patches"]]})
        except Exception as e:
            st.error(str(e))

for k, r in enumerate(st.session_state.results):
    df = r["df"]
    st.divider()
    st.subheader(r["group"])
    c = st.columns(5)
    c[0].metric("Total", len(df))
    c[1].metric("Blur / Low-res", int(df["Status"].str.contains("BLUR|LOW_RES").sum()))
    c[2].metric("Mismatch", int(df["Status"].str.contains("MISMATCH").sum()))
    c[3].metric("Review", int(df["Status"].str.contains("REVIEW").sum()))
    c[4].metric("OK", int((df["Status"] == "OK").sum()))
    fname = f"DAP_QC_{re.sub(r'[^A-Za-z0-9]+', '_', r['group'])[:40]}_{datetime.now():%Y%m%d}.xlsx"
    st.download_button("⬇ Download Excel", r["xlsx"], file_name=fname, key=f"dl{k}",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    tab1, tab2, tab3 = st.tabs(["Mismatch preview", "Blur preview", "All patches"])
    for tab, key in ((tab1, "MISMATCH"), (tab2, "BLUR|LOW_RES")):
        with tab:
            sub = df[df["Status"].str.contains(key)]
            if key == "MISMATCH":
                sub = sub.sort_values("Similarity")
            if sub.empty:
                st.success("Kuch nahi mila 🎉")
            cols = st.columns(8)
            for j, (_, row) in enumerate(sub.head(48).iterrows()):
                cols[j % 8].image(str(Path(r["dir"]) / row["_file"]),
                                  caption=f"#{row['Index']} | sim {row['Similarity']} | blur {row['Blur Score']}")
    with tab3:
        st.dataframe(df.drop(columns=["_file"]), use_container_width=True)
