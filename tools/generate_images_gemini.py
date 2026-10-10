"""Generate one illustration per row of image-requirements.csv with a Gemini image model.

Each image is saved as <out_dir>/<question>.png (same naming as image/), and a manifest
(_generated.csv) records the prompt and model used. Re-running skips rows already generated.

Usage:
    GEMINI_API_KEY=... python3 tools/generate_images_gemini.py [--out image_generated] [--limit N]
                                                              [--only FILE,FILE] [--dry-run]
Env:
    GEMINI_API_KEY       required (unless --dry-run)
    GEMINI_IMAGE_MODEL   optional; defaults to the first available model whose name contains "image"
"""
import argparse, base64, csv, json, os, re, sys, time, urllib.error, urllib.request

API = "https://generativelanguage.googleapis.com/v1beta"
FIELDS = ["row", "subject", "file", "question", "image", "model", "prompt"]
SUBJECTS = {
    "anat": "anatomy", "physio": "physiology", "biochem": "biochemistry", "path": "pathology",
    "pharma": "pharmacology", "micro": "microbiology", "fmt": "forensic medicine", "ophthal": "ophthalmology",
    "ent": "ENT (otorhinolaryngology)", "derm": "dermatology", "psych": "psychiatry", "anae": "anaesthesia",
    "radio": "radiology", "ortho": "orthopaedics", "paeds": "paediatrics", "og": "obstetrics and gynaecology",
    "surg": "surgery", "med": "medicine",
}


def safe_name(q):
    q = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" .")
    while len(q.encode("utf-8")) > 200:
        q = q[:-1]
    return q.strip(" .") or "untitled"


def build_prompt(r):
    subject = SUBJECTS.get(r["subject"], r["subject"])
    desc = r["must_show"].strip()
    if not desc or desc.startswith("(no description"):
        desc = f"An image that shows: {r['answer'].strip() or r['question'].strip()}."
    return (
        f"Create a single clear, medically accurate educational image for this {subject} exam question.\n"
        f"What the image must show: {desc}\n"
        f"Question it accompanies: {r['question'].strip()}\n"
        "Style: textbook-quality medical illustration (or a realistic clinical photo, radiograph, micrograph, "
        "ECG or graph if that is what the description implies). Plain white or neutral background. "
        "If the description mentions arrows, markers or letter/number labels, include exactly those, "
        "and keep any text short and legible. Do not write the answer on the image. No watermarks."
    )


def call(url, body, key, tries=6):
    data = json.dumps(body).encode() if body is not None else None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")[:300]
            if e.code in (429, 500, 502, 503, 504):
                wait = min(120, 10 * 2 ** i)
                print(f"  HTTP {e.code}, retrying in {wait}s: {msg}", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            print(f"  network error {e}, retrying", flush=True)
            time.sleep(5 * (i + 1))
    raise RuntimeError("gave up after retries")


def pick_model(key):
    if os.environ.get("GEMINI_IMAGE_MODEL"):
        return os.environ["GEMINI_IMAGE_MODEL"]
    models = call(f"{API}/models?pageSize=1000", None, key).get("models", [])
    names = [m["name"].split("/", 1)[1] for m in models
             if "image" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]
    if not names:
        sys.exit("No Gemini image-generation model is available to this API key; set GEMINI_IMAGE_MODEL.")
    names.sort(key=lambda n: ("preview" in n, n), reverse=False)
    return names[0]


def generate(model, prompt, key):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    res = call(f"{API}/models/{model}:generateContent", body, key)
    for cand in res.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"]), inline.get("mimeType", "image/png")
    reason = (res.get("promptFeedback") or {}).get("blockReason") or \
        [c.get("finishReason") for c in res.get("candidates", [])]
    raise RuntimeError(f"no image returned ({reason})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="image-requirements.csv")
    ap.add_argument("--out", default="image_generated")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    only = set(filter(None, a.only.split(",")))
    os.makedirs(a.out, exist_ok=True)
    man_path = os.path.join(a.out, "_generated.csv")
    done = {}
    if os.path.exists(man_path):
        done = {m["file"]: m for m in csv.DictReader(open(man_path, encoding="utf-8"))}
    names = {m["image"].rsplit(".", 1)[0] for m in done.values()}

    key = os.environ.get("GEMINI_API_KEY", "")
    if not a.dry_run and not key:
        sys.exit("GEMINI_API_KEY is not set.")
    try:
        model = "dry-run" if a.dry_run else pick_model(key)
    except RuntimeError as e:
        sys.exit(f"Could not reach Gemini with this API key: {e}")
    print(f"model: {model}", flush=True)

    new = not os.path.exists(man_path)
    with open(man_path, "a", newline="", encoding="utf-8") as mf:
        w = csv.DictWriter(mf, fieldnames=FIELDS)
        if new:
            w.writeheader()
        count = failed = 0
        for i, r in enumerate(rows):
            if (only and r["file"] not in only) or r["file"] in done:
                continue
            if a.limit and count >= a.limit:
                break
            count += 1
            base = safe_name(r["question"] or r["answer"] or r["file"])
            stem, n = base, 2
            while stem in names:
                stem = f"{base} ({n})"; n += 1
            prompt = build_prompt(r)
            print(f"[{i+1}/{len(rows)}] {r['file']}", flush=True)
            if a.dry_run:
                print("  " + prompt.replace("\n", "\n  "), flush=True)
                continue
            try:
                blob, mime = generate(model, prompt, key)
            except RuntimeError as e:
                failed += 1
                print(f"  FAILED: {e}", flush=True)
                continue
            ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
            open(os.path.join(a.out, stem + ext), "wb").write(blob)
            names.add(stem)
            w.writerow({"row": r["row"], "subject": r["subject"], "file": r["file"], "question": r["question"],
                        "image": stem + ext, "model": model, "prompt": prompt})
            mf.flush()
            print(f"  -> {stem[:70]}{ext}", flush=True)
        print(f"done: {count} processed, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
