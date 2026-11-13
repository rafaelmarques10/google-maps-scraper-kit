#!/usr/bin/env python3
"""Shows where each field sits in a saved /maps/rpc/listugcposts response, without printing any review text.

    REVIEWS_DEBUG_DIR=/tmp/lp python3 scripts/engine.py start     # collect reviews for one place, then:
    python3 scripts/reviews_probe.py /tmp/lp/listugcposts-1.json

For the first review it prints each position that holds a value, its kind and a label when the value looks
like a rating (1-5), a timestamp, a review link, a contributor id, a review id or free text. Compare with
LAYOUT in engine/reviews.py and fix the positions that differ. Nothing personal is printed: texts show only
their length. Delete the saved files when done: they hold real reviews.
"""
import json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))
import privacy, reviews  # noqa: E402


def label(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        if 1 <= v <= 5:
            return "int 1-5 (nota?)"
        if privacy.from_micros(v):
            return "int timestamp (data?)"
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        if reviews.REVIEW_URL.search(v):
            return "str LINK da review"
        if reviews.CONTRIB.search(v):
            return "str link de perfil (contributor id)"
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}", v):
            return f"str id-like len={len(v)} (review_id?)"
        if re.search(r"\b(ago|atr[aá]s|hace|h[aá])\b", v, re.I) and len(v) < 40:
            return f"str data relativa len={len(v)}"
        return f"str len={len(v)}" + (" (texto?)" if len(v) > 25 else "")
    return type(v).__name__


def walk(node, path, out, depth=0):
    if depth > 9:
        return
    if isinstance(node, list):
        for i, x in enumerate(node):
            walk(x, path + (i,), out, depth + 1)
    elif node is not None:
        out.append((path, label(node)))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    body = open(sys.argv[1], encoding="utf-8").read()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    data = json.loads(body)
    print("próxima página (LAYOUT['next']):", reviews.dig(data, reviews.LAYOUT["next"]) is not None)
    lst = reviews.as_list(data, reviews.LAYOUT["list"])
    print(f"reviews em LAYOUT['list']={reviews.LAYOUT['list']}: {len(lst)}")
    if not lst:
        print("→ lista vazia nessa posição: procure a lista de reviews no topo da resposta:")
        for i, x in enumerate(data if isinstance(data, list) else []):
            print(f"   posição {i}: {type(x).__name__}" + (f" len={len(x)}" if isinstance(x, list) else ""))
        return
    out = []
    walk(lst[0], (), out)
    for path, lab in out:
        if lab not in ("int", "bool", "float") or len(path) < 6:
            print(f"  {path}: {lab}")
    r = reviews.parse_review(lst[0])
    print("\nlido com o LAYOUT atual:", {k: (v if k in ("nota", "data", "data_precisao", "url_exata", "tem_resposta") else bool(v))
                                          for k, v in (r or {}).items()} or "NADA (id ou nota fora do lugar)")


if __name__ == "__main__":
    main()
