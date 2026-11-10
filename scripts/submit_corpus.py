"""Send the scraped corpus in data/raw/ to the API as one batch.

    python scripts/submit_corpus.py               # all 249 unique postings
    python scripts/submit_corpus.py --limit 10    # a small trial run first

Each file is a provenance header, a line with `---`, then the posting text.
Only the text after `---` is sent: the header holds the job board's own title
and company, which the model must read from the posting itself.
"""

import argparse
import hashlib
from pathlib import Path

import httpx

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def load_corpus() -> list[dict]:
    postings, seen = [], set()
    for path in sorted(RAW_DIR.glob("*/*.txt")):
        text = path.read_text(encoding="utf-8")
        _, sep, body = text.partition("\n---\n")
        body = body.strip()
        if not sep or not body:
            continue
        digest = hashlib.sha256(body.encode()).hexdigest()
        if digest in seen:  # the same posting listed once per city
            continue
        seen.add(digest)
        postings.append({"source_key": f"{path.parent.name}/{path.stem}", "body": body})
    return postings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=0, help="only the first N postings")
    parser.add_argument("--name", default=None, help="batch name")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    postings = load_corpus()
    if args.limit:
        postings = postings[: args.limit]
    name = args.name or f"corpus ({len(postings)})"

    response = httpx.post(f"{args.api}/api/batches",
                          json={"name": name, "postings": postings}, timeout=60)
    response.raise_for_status()
    batch_id = response.json()["id"]
    print(f"batch {batch_id}: {len(postings)} postings queued")
    print(f"watch it at {args.api}/batches/{batch_id}")


if __name__ == "__main__":
    main()
