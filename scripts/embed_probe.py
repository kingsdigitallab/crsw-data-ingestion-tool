"""Embedding probe: why does a short question not find the passage it came from?

Reads PROMOTER_LLM_BASE_URL / PROMOTER_LLM_API_KEY from .env.promoter (repo
root), lists the platform's models, and for each embedding alias found prints
the cosine similarity between one 350-word passage (1956 Convention, art. 1)
and a handful of queries: the passage's own text, one verbatim sentence from
it, a paraphrase with no shared words, and a control that matches nothing.
Each query is tried bare and with the prefixes the common retrieval models
expect; each pair is scored at full length and cut to 1024 numbers, the way
the promoter stores vectors. Stdlib only. Diagnostic, not part of the tool:

    python scripts/embed_probe.py              # every embedding alias found
    python scripts/embed_probe.py arc:embedvl  # just one
    python scripts/embed_probe.py arc:embed --split 150/30   # also split the passage
                                               # into pieces of 150 words, each starting
                                               # 30 before the last ended, and score the
                                               # queries against every piece (the
                                               # passage-length question, 16 Oct 2026)
"""
import json, math, sys, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASSAGE = ("institutions and practices, where they still exist and whether or not they are "
           "covered by the definition of slavery contained in article 1 of the Slavery Convention "
           "signed at Geneva on 25 September 1926: (a) Debt bondage, that is to say, the status or "
           "condition arising from a pledge by a debtor of his personal services or of those of a "
           "person under his control as security for a debt, if the value of those services as "
           "reasonably assessed is not applied towards the liquidation of the debt or the length and "
           "nature of those services are not respectively limited and defined; (b) Serfdom, that is "
           "to say, the condition or status of a tenant who is by law, custom or agreement bound to "
           "live and labour on land belonging to another person and to render some determinate "
           "service to such other person, whether for reward or not, and is not free to change his "
           "status; (c) Any institution or practice whereby: (i) A woman, without the right to "
           "refuse, is promised or given in marriage on payment of a consideration in money or in "
           "kind to her parents, guardian, family or any other person or group; or (ii) The husband "
           "of a woman, his family, or his clan, has the right to transfer her to another person for "
           "value received or otherwise; or (iii) A woman on the death of her husband is liable to "
           "be inherited by another person; (d) Any institution or practice whereby a child or young "
           "person under the age of 18 years is delivered by either or both of his natural parents "
           "or by his guardian to another person, whether for reward or not, with a view to the "
           "exploitation of the child or young person or of his labour. Article 2 With a view to "
           "bringing to an end the institutions and practices mentioned in article 1 (c) of this "
           "Convention, the States Parties undertake to prescribe, where appropriate, suitable "
           "minimum ages of marriage, to encourage the use of facilities whereby the consent of both "
           "parties to a marriage may be")

# A second, unrelated passage so we can see the gap between right and wrong,
# not just the absolute number (UNSC 2331 preamble, trafficking and terrorism).
OTHER = ("Reiterating the critical importance of all Member States fully implementing relevant "
         "Security Council resolutions, including resolutions 2195 (2014) and 2253 (2015), which "
         "express concern that terrorists benefit from transnational organized crime in some "
         "regions, including from the trafficking of arms, persons, drugs, and artefacts and from "
         "the illicit trade in natural resources including gold and other precious metals and "
         "stones, minerals, wildlife, charcoal and oil, as well as from kidnapping for ransom and "
         "other crimes including extortion and bank robbery, Recognizing that trafficking in "
         "persons in areas affected by armed conflict and post-conflict situations can be for the "
         "purpose of various forms of exploitation, including exploitation of the prostitution of "
         "others or other forms of sexual exploitation, forced labour, slavery or practices similar "
         "to slavery, servitude or the removal of organs")

QUERIES = [
    ("own text (first 450 chars)", PASSAGE[:450]),
    ("verbatim sentence", "A woman on the death of her husband is liable to be inherited by another person"),
    ("paraphrase, no shared words", "widows passed on to a relative of the deceased"),
    ("control, matches nothing", "satellite imagery of cropland"),
]

PREFIXES = [
    ("bare", "{q}", "{p}"),
    ("qwen3", "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: {q}", "{p}"),
    ("e5/nomic", "query: {q}", "passage: {p}"),
    ("bge", "Represent this sentence for searching relevant passages: {q}", "{p}"),
    ("jina", "Query: {q}", "Passage: {p}"),
]

CUT = 1024


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
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def embed(base, key, model, texts):
    body = call(base, key, "/embeddings", {"model": model, "input": texts})
    data = sorted(body["data"], key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


def cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / na / nb


def split_words(text, words, overlap):
    """The promoter's rule (promoter/passages.py chunk): pieces of `words`
    words, each starting `overlap` words before the previous one ended."""
    ws = text.split()
    step = max(1, words - overlap)
    out, start = [], 0
    while start < len(ws):
        out.append(" ".join(ws[start:start + words]))
        if start + words >= len(ws):
            break
        start += step
    return out


def split_table(base, key, alias, words, overlap):
    """Score every query against each piece of PASSAGE, bare and in the
    live (qwen3) form, so the right size can be chosen before a re-embed."""
    pieces = split_words(PASSAGE, words, overlap)
    print("  split %d/%d: %d pieces" % (words, overlap, len(pieces)))
    for i, piece in enumerate(pieces):
        print("    piece %d (%d words): %s ..." % (i, len(piece.split()), " ".join(piece.split()[:9])))
    for name, qf, pf in PREFIXES[:2]:
        vecs = embed(base, key, alias, [pf.format(p=p) for p in pieces]
                     + [qf.format(q=q) for _, q in QUERIES])
        pv, qs = vecs[:len(pieces)], vecs[len(pieces):]
        print("  %-12s %-30s %s" % (name, "query", " ".join("piece%d" % i for i in range(len(pieces)))))
        for (label, _), qv in zip(QUERIES, qs):
            print("  %-12s %-30s %s" % (name, label,
                                        " ".join("%6.3f" % cos(qv[:CUT], p[:CUT]) for p in pv)))
        print()


def main():
    env = env_file(ROOT / ".env.promoter")
    base, key = env.get("PROMOTER_LLM_BASE_URL"), env.get("PROMOTER_LLM_API_KEY")
    if not base or not key:
        sys.exit("PROMOTER_LLM_BASE_URL and PROMOTER_LLM_API_KEY not found in .env.promoter")

    args = sys.argv[1:]
    split = None
    if "--split" in args:
        i = args.index("--split")
        w, o = args[i + 1].split("/")
        split = (int(w), int(o))
        del args[i:i + 2]

    models = call(base, key, "/models").get("data", [])
    print("models on the platform:")
    for m in models:
        extra = {k: m[k] for k in m if k not in ("id", "object", "created", "owned_by")}
        print("  %-20s %s" % (m.get("id"), json.dumps(extra) if extra else ""))
    wanted = args or [m["id"] for m in models if "embed" in str(m.get("id", "")).lower()]
    if not wanted:
        sys.exit("no embedding alias found; pass one on the command line")

    for alias in wanted:
        print("\n==== %s" % alias)
        try:
            probe = embed(base, key, alias, ["dimension check"])[0]
        except urllib.error.HTTPError as e:
            print("  embed failed: HTTP %s %s" % (e.code, e.read()[:200]))
            continue
        full = len(probe)
        print("  native dimension: %d  (promoter stores the first %d)" % (full, CUT))
        print("  %-12s %-30s %8s %8s %8s %8s" % ("prefix", "query", "right", "right@%d" % CUT, "wrong", "wrong@%d" % CUT))
        for name, qf, pf in PREFIXES:
            try:
                vecs = embed(base, key, alias, [pf.format(p=PASSAGE), pf.format(p=OTHER)]
                             + [qf.format(q=q) for _, q in QUERIES])
            except urllib.error.HTTPError as e:
                print("  %-12s embed failed: HTTP %s" % (name, e.code))
                continue
            right, wrong, qs = vecs[0], vecs[1], vecs[2:]
            for (label, _), qv in zip(QUERIES, qs):
                print("  %-12s %-30s %8.3f %8.3f %8.3f %8.3f" % (
                    name, label, cos(qv, right), cos(qv[:CUT], right[:CUT]),
                    cos(qv, wrong), cos(qv[:CUT], wrong[:CUT])))
            print()
        if split:
            split_table(base, key, alias, *split)


if __name__ == "__main__":
    main()
