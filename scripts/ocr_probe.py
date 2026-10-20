"""OCR probe: can the platform's chat alias read a page image, and how well?

Reads PROMOTER_LLM_BASE_URL / PROMOTER_LLM_API_KEY from .env.promoter (repo
root), lists the platform's models (naming the ones whose listing says they
take images), renders the first pages of a PDF that has no text layer to
JPEG, sends each page to /chat/completions as a data URL with the transcribe
prompt the promoter would use, and prints per page the HTTP status, the
seconds taken, the usage tokens and the text that came back. An HTTP error
prints the status and the body, which is where "images not supported" would
appear. Decides vision model versus an OCR engine before anything is built
(20 October 2026). Needs pypdfium2 and Pillow (wheels, no system packages):

    .venv/Scripts/python -m pip install pypdfium2 Pillow
    python scripts/ocr_probe.py PATH/TO/scan.pdf                 # arc:lite, 2 pages, 150 dpi
    python scripts/ocr_probe.py scan.pdf --alias arc:nano --pages 1 --dpi 120

Diagnostic, not part of the tool. The key is read, never printed.
"""
import base64, io, json, sys, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMPT = ("Transcribe every word printed on this page exactly as it appears, in "
          "reading order, as plain text. Do not summarise, correct, translate or "
          "add anything. If the page has no readable text, answer with the single "
          "word NONE.")


def env_file(path):
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def call(base, key, path, payload=None):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def page_images(pdf_path, n, dpi):
    """The first `n` pages as JPEG bytes (quality 85), the way the promoter
    would render them."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    out = []
    for i in range(min(n, len(doc))):
        bitmap = doc[i].render(scale=dpi / 72.0)
        image = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        out.append((image.size, buf.getvalue()))
    return out


def transcribe(base, key, alias, jpeg):
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return call(base, key, "/chat/completions", {
        "model": alias,
        "temperature": 0,
        "max_tokens": 3000,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]},
        ],
    })


def main():
    env = env_file(ROOT / ".env.promoter")
    base, key = env.get("PROMOTER_LLM_BASE_URL"), env.get("PROMOTER_LLM_API_KEY")
    if not base or not key:
        sys.exit("PROMOTER_LLM_BASE_URL and PROMOTER_LLM_API_KEY not found in .env.promoter")

    args = sys.argv[1:]
    alias, pages, dpi = "arc:lite", 2, 150
    if "--alias" in args:
        i = args.index("--alias"); alias = args[i + 1]; del args[i:i + 2]
    if "--pages" in args:
        i = args.index("--pages"); pages = int(args[i + 1]); del args[i:i + 2]
    if "--dpi" in args:
        i = args.index("--dpi"); dpi = int(args[i + 1]); del args[i:i + 2]
    if len(args) != 1:
        sys.exit("usage: ocr_probe.py PDF [--alias arc:lite] [--pages 2] [--dpi 150]")
    pdf = Path(args[0])

    models = call(base, key, "/models").get("data", [])
    print("models on the platform (those that say they take images marked *):")
    for m in models:
        flat = json.dumps(m).lower()
        vision = ("vision" in flat and '"supports_vision": false' not in flat) or "image" in flat
        print("  %s %s" % ("*" if vision else " ", m.get("id")))

    print("\nrendering the first %d page(s) of %s at %d dpi" % (pages, pdf.name, dpi))
    images = page_images(pdf, pages, dpi)
    for i, (size, jpeg) in enumerate(images, 1):
        print("  page %d: %dx%d px, %d bytes JPEG" % (i, size[0], size[1], len(jpeg)))

    print("\n==== %s" % alias)
    for i, (_, jpeg) in enumerate(images, 1):
        t0 = time.time()
        try:
            body = transcribe(base, key, alias, jpeg)
        except urllib.error.HTTPError as e:
            print("\n-- page %d: HTTP %s after %.1fs\n%s" % (
                i, e.code, time.time() - t0, e.read().decode("utf-8", "replace")[:1000]))
            continue
        secs = time.time() - t0
        usage = body.get("usage") or {}
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print("\n-- page %d: HTTP 200 in %.1fs; usage %s; %d chars, %d words" % (
            i, secs, json.dumps(usage), len(text), len(text.split())))
        print(text)


if __name__ == "__main__":
    main()
