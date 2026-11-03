"""Measure Prisma accuracy on CONSENTED recordings, per noise condition, with and without keyterms.

Put audio in eval/recordings/ (gitignored, never published) and describe each clip in
eval/manifest.csv:
    file,condition,reference,amounts
    d1_pump_01.ogg,fuel_pump,"chaar hazaar paanch sau ka diesel daala",4500
    d2_cab_03.ogg,cab_moving,"toll pe teen sau diye aur khane ke do sau",300|200

Run (uses real Gnani credits, 2 calls per clip):
    python eval/run_eval.py --manifest eval/manifest.csv
Writes eval/results/results.csv and eval/results/summary.md (aggregates only: safe to publish).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from saarthi.speech import get_speech  # noqa: E402

NUM_WORDS = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6,
             "saat": 7, "aath": 8, "nau": 9, "das": 10, "bees": 20, "tees": 30, "chalis": 40, "pachas": 50,
             "sau": 100, "hazaar": 1000, "hazar": 1000, "lakh": 100000}


def normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    text = re.sub(r"(?<=\d),(?=\d)", "", text)  # 4,500 -> 4500
    text = re.sub(r"[^\w\sऀ-ॿ]", " ", text)
    return text.split()


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / len(r)


DEV = str.maketrans("०१२३४५६७८९", "0123456789")


def amounts_in(text: str) -> set[int]:
    t = text.translate(DEV)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return {int(x) for x in re.findall(r"\d+", t)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="eval/manifest.csv")
    ap.add_argument("--audio-dir", default="eval/recordings")
    ap.add_argument("--out", default="eval/results")
    ap.add_argument("--no-baseline", action="store_true", help="skip the without-keyterms run (half the credits)")
    a = ap.parse_args()
    sp = get_speech()
    if sp.mode != "gnani":
        sys.exit("SPEECH_MODE must be 'gnani' for evaluation (mock results are meaningless).")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    results = []
    variants = [("keyterms", True)] + ([] if a.no_baseline else [("baseline", False)])
    for row in rows:
        path = Path(a.audio_dir) / row["file"]
        expected = {int(x) for x in row.get("amounts", "").split("|") if x.strip()}
        for name, kt in variants:
            try:
                res = sp.transcribe_sync(path, use_keyterms=kt)
                hyp = res.get("transcript", "")
            except Exception as e:
                hyp, res = "", {"error": str(e)}
            got = amounts_in(hyp)
            results.append({
                "file": row["file"], "condition": row["condition"], "variant": name,
                "wer": round(wer(row["reference"], hyp), 3),
                "amounts_expected": len(expected), "amounts_correct": len(expected & got),
                "latency_s": res.get("latency_s"), "hypothesis": hyp, "error": res.get("error", ""),
            })
            print(f"{row['file']:<28} {name:<9} WER={results[-1]['wer']:.2f} amounts {len(expected & got)}/{len(expected)}")
            time.sleep(0.3)
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    agg = defaultdict(lambda: {"n": 0, "wer": 0.0, "exp": 0, "ok": 0, "lat": 0.0})
    for r in results:
        g = agg[(r["condition"], r["variant"])]
        g["n"] += 1; g["wer"] += r["wer"]; g["exp"] += r["amounts_expected"]; g["ok"] += r["amounts_correct"]
        g["lat"] += r["latency_s"] or 0
    lines = ["| Condition | Variant | Clips | Avg WER | Amount accuracy | Avg latency (s) |", "|---|---|---|---|---|---|"]
    for (cond, var), g in sorted(agg.items()):
        acc = f"{100 * g['ok'] / g['exp']:.0f}% ({g['ok']}/{g['exp']})" if g["exp"] else "-"
        lines.append(f"| {cond} | {var} | {g['n']} | {g['wer'] / g['n']:.2f} | {acc} | {g['lat'] / g['n']:.2f} |")
    (out / "summary.md").write_text("# Prisma accuracy by noise condition\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
