# -*- coding: utf-8 -*-
"""Read-only audit: frontend api* calls vs backend OpenAPI routes."""
import json, re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = json.load(open(ROOT / "work" / "_openapi.json", encoding="utf-8"))

routes = []
for path, ops in SPEC["paths"].items():
    for method in ops:
        routes.append((method.upper(), path))

route_re = []
for method, path in routes:
    pat = re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
    # re.escape turned {x} into \{x\}; rebuild manually
    segs = []
    for part in path.split("/"):
        if part.startswith("{") and part.endswith("}"):
            segs.append("[^/]+")
        else:
            segs.append(re.escape(part))
    route_re.append((method, path, re.compile("^" + "/".join(segs) + "$")))

CALL = re.compile(r"\b(api|apiGet|apiPost|apiPostJson|apiJson)\(")


def extract_calls(src):
    out = []
    for m in CALL.finditer(src):
        name = m.group(1)
        i = m.end()
        depth = 1
        quote = None
        esc = False
        while i < len(src) and depth:
            ch = src[i]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            else:
                if ch in "'\"`":
                    quote = ch
                elif ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        arg = src[m.end():i]
        line = src.count("\n", 0, m.start()) + 1
        out.append((name, arg, line))
    return out


def split_args(arg):
    parts, depth, quote, esc, cur = [], 0, None, False, []
    for ch in arg:
        if quote:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def norm_path(expr):
    """Turn a JS path expression into a template with {} for dynamic segments."""
    e = expr.strip()
    # strip outer parens
    e = re.sub(r"^\((.*)\)$", r"\1", e, flags=re.S)
    # 'a' + expr + 'b'  ->  a{}b ; keep literal chunks, drop expressions
    chunks = re.findall(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", e)
    if chunks:
        lit = "".join(next(g for g in (a, b, c) if g is not None) for a, b, c in chunks)
        # template literal ${...} -> {}
        lit = re.sub(r"\$\{[^}]*\}", "{}", lit)
        # count non-literal concatenations (top-level +) to place {}
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "", e)
        n_dyn = len([p for p in re.split(r"\+", stripped) if p.strip()])
        if n_dyn and "{}" not in lit:
            # place a {} where each dynamic join happened: best effort -> append marker per gap
            # Rebuild by splitting original on literals
            seq = []
            pos = 0
            for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", e):
                between = e[pos:m.start()]
                if between.strip(" +"):
                    seq.append("{}")
                seq.append(next(g for g in m.groups() if g is not None))
                pos = m.end()
            tail = e[pos:]
            if tail.strip(" +"):
                seq.append("{}")
            lit = "".join(seq)
    else:
        lit = e
    # query string: keep only path part
    lit = lit.split("?")[0].split("#")[0]
    lit = re.sub(r"\{+", "{", lit)
    lit = re.sub(r"\}+", "}", lit)
    return lit


def method_of(name, args):
    if name in ("apiPost", "apiPostJson"):
        return "POST"
    if name == "apiJson":
        if len(args) > 1:
            m = re.search(r"""['"]([A-Z]+)['"]""", args[1])
            if m:
                return m.group(1).upper()
        return "?"
    if name == "apiGet":
        return "GET"
    # api(path, options)
    if len(args) > 1:
        m = re.search(r"""method\s*:\s*['"]([A-Za-z]+)['"]""", args[1])
        if m:
            return m.group(1).upper()
    return "GET"


def match_route(method, path):
    hits = [p for m, p, rx in route_re if m == method and rx.match(path)]
    if hits:
        return hits
    # try any method to detect method mismatch
    anym = [(m, p) for m, p, rx in route_re if rx.match(path)]
    return anym


def main():
    files = sorted((ROOT / "frontend" / "src").rglob("*.js"))
    seen = {}
    for f in files:
        src = f.read_text(encoding="utf-8")
        for name, arg, line in extract_calls(src):
            args = split_args(arg)
            if not args or not args[0].strip():
                continue
            path = norm_path(args[0])
            if not path.startswith("/api"):
                continue
            method = method_of(name, args)
            key = (method, path)
            seen.setdefault(key, []).append(f"{f.relative_to(ROOT)}:{line}")

    unmatched, matched = [], []
    for (method, path), locs in sorted(seen.items()):
        hits = match_route(method, path)
        if hits and isinstance(hits[0], str):
            matched.append((method, path, hits[0], locs))
        else:
            unmatched.append((method, path, hits, locs))

    print(f"frontend api call sites (unique method+path): {len(seen)}")
    print(f"matched: {len(matched)}  unmatched: {len(unmatched)}")
    print("\n== UNMATCHED (method mismatch or missing route) ==")
    for method, path, hits, locs in unmatched:
        alt = ", ".join(f"{m} {p}" for m, p in hits) if hits else "NO ROUTE"
        print(f"{method} {path}\n    alt: {alt}\n    at: {', '.join(locs[:3])}")

    used_routes = {(h[0], h[2]) for h in matched}
    print("\n== BACKEND ROUTES NOT CALLED BY FRONTEND ==")
    for method, path in sorted(routes):
        if (method, path) not in used_routes:
            print(f"{method} {path}")

    print("\n== MATCHED ==")
    for method, path, hit, locs in matched:
        print(f"{method} {path} -> {hit}")


if __name__ == "__main__":
    main()
