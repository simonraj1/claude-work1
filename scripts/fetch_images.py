#!/usr/bin/env python3
"""Download one image per row of image-requirements.csv into image/.

Each image is saved as "<question>.jpg" (question text made filesystem-safe).
Images are searched on Wikimedia Commons (freely licensed) using the row's
answer, then the keywords in its planned filename. Every result is recorded in
image/_manifest.csv with its source URL, license and author so it can be
reviewed and attributed.

Auto-matched images only approximate `must_show` (arrows, labels and specific
views are not guaranteed), so review the manifest before using them.

Usage:
    python3 scripts/fetch_images.py            # download missing images
    python3 scripts/fetch_images.py --dry-run  # only write planned names
    python3 scripts/fetch_images.py --limit 20 # first 20 rows only
"""
import argparse
import csv
import io
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "image-requirements.csv"
OUT_DIR = ROOT / "image"
MANIFEST = OUT_DIR / "_manifest.csv"

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "image-requirements-fetcher/1.0 (https://github.com/simonraj1/claude-work1)"
MAX_NAME = 180  # characters, before the extension

SUBJECT_HINTS = {
    "anat": "anatomy", "physio": "physiology", "biochem": "biochemistry",
    "path": "histopathology", "pharma": "", "micro": "microscopy",
    "fmt": "forensic", "ophthal": "eye", "ent": "", "derm": "skin",
    "psych": "", "anae": "anesthesia", "radio": "radiograph",
    "ortho": "x-ray", "paeds": "child", "og": "", "surg": "surgery",
    "med": "",
}

# Text pasted in from the source books that should not end up in file names.
JUNK = [
    r"Click Here to Buy book on (A|a)mazon\.?",
    r"\((AIIMS|NEET)[^)]*\)?",
]


def safe_name(text):
    """Turn a question into a file name that works on Windows, macOS and Linux."""
    for pat in JUNK:
        text = re.sub(pat, "", text)
    text = text.replace("/", "-").replace("\\", "-").replace(":", " -")
    text = text.replace('"', "'").replace("“", "'").replace("”", "'")
    text = re.sub(r'[*?<>|\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if len(text) > MAX_NAME:
        text = text[:MAX_NAME].rsplit(" ", 1)[0].rstrip(" .-")
    return text or "untitled"


def plan_names(rows):
    """Map each row to a unique file name; duplicate questions get the row id."""
    names = [safe_name(r["question"]) for r in rows]
    counts = {}
    for n in names:
        counts[n.lower()] = counts.get(n.lower(), 0) + 1
    planned = []
    for r, n in zip(rows, names):
        if counts[n.lower()] > 1:
            n = f"{n} ({r['subject']}-{r['row']})"
        planned.append(n + ".jpg")
    return planned


def is_meaningful(answer):
    """False for option codes like 'B', '2', '1-D, 2-C', 'a, b & c'."""
    words = re.findall(r"[A-Za-z]{4,}", answer)
    return bool(words)


SHOWN_PATTERNS = [
    r"(?:marked|labell?ed|highlighted|indicated|given|shown|arrow-marked)"
    r" (?:structure|area|nerve|muscle|layer|lesion|cell|instrument|region|bone)"
    r"(?: in [^,.]*?)? (?:is|are|represents|corresponds to) (?:the |a |an )?([^.,;(]+)",
    r"(?:arrow|label \w+) (?:is )?(?:pointing|points) (?:on |to(?:wards)? )?(?:the |a |an )?([^.,;(]+)",
    r"(?:image|picture|figure|slide|diagram|X-ray|radiograph|scan)"
    r" (?:shows|displays|depicts|illustrates|indicates|is of|is showing|highlights)"
    r" (?:the |a |an )?(?:classic |characteristic |typical )?([^.,;(]+)",
]


GENERIC = {
    "left", "right", "label", "labels", "multiple", "several", "large", "small",
    "well-defined", "pathognomonic", "finding", "findings", "features", "image",
    "structure", "area", "region", "classic", "typical", "characteristic",
    "significant", "clinically", "important", "common", "critical", "step",
    "result", "view", "appearance", "pattern",
}
# Words that may stay in a phrase but cannot be its only content.
WEAK = {
    "anterior", "posterior", "lateral", "medial", "superior", "inferior",
    "uppermost", "aspect", "nerve", "shaft", "triangular", "extension", "key",
    "transient", "histology", "slide", "structures", "both", "lower", "upper",
}


def shown_in_image(must_show):
    """Best guess at what the image itself shows, e.g. 'gluteus medius muscle'."""
    for pat in SHOWN_PATTERNS:
        m = re.search(pat, must_show, re.IGNORECASE)
        if not m:
            continue
        phrase = m.group(1).split(":")[-1].strip()
        phrase = re.split(r"\s+(?:which|that|with|where|located|in the|on the|of the|and)\s+",
                          phrase)[0]
        words = [w for w in phrase.split() if w.lower() not in GENERIC]
        if any(len(w) >= 4 and w.lower() not in WEAK for w in words):
            return " ".join(words[:6])
    return ""


def queries_for(row):
    hint = SUBJECT_HINTS.get(row["subject"], "")
    answer = re.sub(r"\(Ref:.*?\)|\s\d{3}\.?$", "", row["answer"]).strip(" .")
    if not answer and len(row["question"]) < 40:
        answer = row["question"]  # a few rows have the answer in this column
    slug = re.sub(r"^[a-z]+-\d+-|\.jpg$", "", row["file"]).replace("-", " ")
    shown = shown_in_image(row["must_show"])
    shown_q = [f"{shown} {hint}".strip(), shown] if shown else []
    answer_q = [f"{answer} {hint}".strip(), answer] if is_meaningful(answer) else []
    # A one-word guess ("child", "pterion") is less reliable than the answer.
    if len(shown.split()) >= 2:
        out = shown_q + answer_q
    else:
        out = answer_q + shown_q
    if slug and slug not in ("available recall", "available source sheet"):
        out += [f"{slug} {hint}".strip(), slug]
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq


def search_commons(session, query):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": 5, "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata", "iiurlwidth": 1024,
    }
    resp = session.get(API, params=params, timeout=30)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        meta = info.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", ""))
        return {
            "url": info.get("thumburl") or info["url"],
            "page": info.get("descriptionurl", ""),
            "license": meta.get("LicenseShortName", {}).get("value", ""),
            "artist": artist.strip(),
        }
    return None


def save_jpeg(data, path):
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(path, "JPEG", quality=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between rows")
    args = ap.parse_args()

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    names = plan_names(rows)
    OUT_DIR.mkdir(exist_ok=True)

    old = {}
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8", newline="") as f:
            old = {(r["subject"], r["row"]): r for r in csv.DictReader(f)}

    session = None
    if not args.dry_run:
        import requests
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

    fields = ["subject", "row", "priority", "image", "status", "query",
              "source_url", "source_page", "license", "artist", "must_show"]
    results = []
    todo = rows if args.limit is None else rows[:args.limit]
    for i, (row, name) in enumerate(zip(rows, names)):
        rec = {"subject": row["subject"], "row": row["row"],
               "priority": row["priority"], "image": name,
               "must_show": row["must_show"]}
        prev = old.get((row["subject"], row["row"]), {})
        for k in ("query", "source_url", "source_page", "license", "artist"):
            rec[k] = prev.get(k, "")
        path = OUT_DIR / name

        if path.exists():
            rec["status"] = prev.get("status") or "downloaded"
        elif args.dry_run or i >= len(todo):
            rec["status"] = "pending"
        else:
            rec["status"] = "not_found"
            for q in queries_for(row):
                try:
                    hit = search_commons(session, q)
                    if not hit:
                        continue
                    img = session.get(hit["url"], timeout=60)
                    img.raise_for_status()
                    save_jpeg(img.content, path)
                except Exception as e:  # keep going; the manifest records it
                    rec["status"] = f"error: {e}"[:200]
                    break
                rec.update(status="downloaded", query=q, source_url=hit["url"],
                           source_page=hit["page"], license=hit["license"],
                           artist=hit["artist"])
                break
            print(f"[{i + 1}/{len(todo)}] {rec['status']:<10} {name}", flush=True)
            time.sleep(args.delay)
        results.append(rec)

    with open(MANIFEST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    done = sum(r["status"] == "downloaded" for r in results)
    print(f"{done}/{len(results)} images present; manifest: {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
