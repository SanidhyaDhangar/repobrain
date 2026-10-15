"""Ingest a public GitHub repo into its own FalkorDB graph.

Code structure comes from the Python AST (files, functions, calls, imports).
History comes from GitHub (merged PRs, the issues they fixed, open issues, and the people behind them).
"""
import ast
import hashlib
import io
import os
import re
import sys
import tarfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from graph import graph

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages", "build", "dist", "__pycache__"}
MAX_PRS = int(os.environ.get("MAX_PRS", 200))
MAX_TARBALL = 150_000_000

SCHEMA = [
    "CREATE INDEX FOR (n:File) ON (n.path)",
    "CREATE INDEX FOR (n:Function) ON (n.qname)",
    "CREATE INDEX FOR (n:Function) ON (n.name)",
    "CREATE INDEX FOR (n:Person) ON (n.login)",
    "CREATE INDEX FOR (n:PR) ON (n.number)",
    "CREATE INDEX FOR (n:Issue) ON (n.number)",
    "CALL db.idx.fulltext.createNodeIndex('PR', 'title', 'body')",
    "CALL db.idx.fulltext.createNodeIndex('Issue', 'title', 'body')",
    "CALL db.idx.fulltext.createNodeIndex('Fact', 'text')",
]


def gh():
    tok = os.environ.get("GITHUB_TOKEN")
    return httpx.Client(base_url="https://api.github.com", timeout=120, follow_redirects=True,
                        headers={"Authorization": f"Bearer {tok}"} if tok else {})


# ---------- code ----------

def fingerprint(node) -> str:
    """Hash of the AST, so formatting and comments don't count as a change but code does."""
    return hashlib.sha1(ast.dump(node).encode()).hexdigest()[:12]


def module_of(path: str) -> str:
    parts = path[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def parse_code(tar: bytes):
    """Return (files, functions, raw calls, raw imports) for every .py file in a repo tarball (read in memory)."""
    files, funcs, calls, imports = [], [], set(), defaultdict(set)
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".py") or m.size > 1_000_000:
                continue
            path = m.name.split("/", 1)[1]  # strip the "owner-repo-sha/" prefix
            if SKIP_DIRS & set(path.split("/")):
                continue
            src = tf.extractfile(m).read()
            try:
                tree = ast.parse(src)
            except (SyntaxError, ValueError):
                continue
            files.append({"path": path, "loc": src.count(b"\n"), "test": "test" in path.lower(), "hash": fingerprint(tree)})

            def add(fn, local):
                qname = f"{path}::{local}"
                funcs.append({"qname": qname, "name": fn.name, "path": path, "line": fn.lineno,
                              "end_line": fn.end_lineno, "doc": (ast.get_docstring(fn) or "").split("\n")[0][:200],
                              "hash": fingerprint(fn)})
                for c in ast.walk(fn):
                    if isinstance(c, ast.Call):
                        f = c.func
                        callee = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                        if callee and callee != fn.name:
                            calls.add((qname, callee))

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(node, node.name)
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            add(sub, f"{node.name}.{sub.name}")

            mod = module_of(path)
            pkg = mod.split(".") if path.endswith("__init__.py") else mod.split(".")[:-1]
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    imports[path].update(a.name for a in n.names)
                elif isinstance(n, ast.ImportFrom):
                    base = n.module or ""
                    if n.level:  # relative import: walk up from this file's package
                        up = pkg[:len(pkg) - n.level + 1]
                        base = ".".join(up + ([n.module] if n.module else []))
                    imports[path].add(base)
                    imports[path].update(f"{base}.{a.name}" for a in n.names)
    return files, funcs, calls, imports


def resolve(files, funcs, calls, imports):
    """Turn raw names into File->File IMPORTS and Function->Function CALLS edges."""
    # ponytail: imports resolved by module name (with an optional leading 'src.'), no sys.path emulation
    by_module = {}
    for f in files:
        mod = module_of(f["path"])
        by_module[mod] = f["path"]
        by_module.setdefault(mod.removeprefix("src."), f["path"])
    file_imports = {(p, by_module[m]) for p, mods in imports.items() for m in mods if m in by_module and by_module[m] != p}
    imported = defaultdict(set)
    for a, b in file_imports:
        imported[a].add(b)

    # ponytail: calls resolved by bare name (same file > imported file > unique in repo); upgrade path is jedi/pyright
    by_name = defaultdict(list)
    for fn in funcs:
        by_name[fn["name"]].append(fn)
    call_edges = set()
    for caller, callee in calls:
        cands = by_name.get(callee)
        if not cands:
            continue
        here = caller.split("::")[0]
        pick = ([c for c in cands if c["path"] == here] or [c for c in cands if c["path"] in imported[here]]
                or (cands if len(cands) == 1 else []))
        call_edges.update((caller, c["qname"]) for c in pick if c["qname"] != caller)
    return [list(e) for e in file_imports], [list(e) for e in call_edges]


# ---------- history ----------

PRS_QUERY = """query($owner: String!, $name: String!, $cursor: String) { repository(owner: $owner, name: $name) {
  pullRequests(first: 50, after: $cursor, states: MERGED, orderBy: {field: CREATED_AT, direction: DESC}) {
    pageInfo { hasNextPage endCursor }
    nodes { number title body url mergedAt author { login }
      reviews(first: 20) { nodes { author { login } } }
      closingIssuesReferences(first: 5) { nodes { number title body url state createdAt author { login } } } } } } }"""

ISSUES_QUERY = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  issues(first: 100, states: OPEN, orderBy: {field: CREATED_AT, direction: DESC}) {
    nodes { number title body url state createdAt author { login } } } } }"""


def graphql(c, query, **vars):
    r = c.post("/graphql", json={"query": query, "variables": vars})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]["repository"]


def login(x):
    return (x or {}).get("login") or "ghost"


def issue_row(i):
    return {"number": i["number"], "title": i["title"], "body": (i["body"] or "")[:3000], "url": i["url"],
            "state": i["state"], "created_at": i["createdAt"], "author": login(i["author"])}


def changed_functions(patch: str, funcs_in_file):
    """Functions a merged PR changed: named in a hunk header's scope context, or whose `def` line changed."""
    # ponytail: name match, not line ranges (line numbers drift after merge); open PRs use changed_lines instead
    lines = (patch or "").splitlines()
    scopes = " ".join(l.split("@@")[-1] for l in lines if l.startswith("@@"))
    changed = "\n".join(l for l in lines if l[:1] in "+-")
    return [f["qname"] for f in funcs_in_file if re.search(rf"\b{re.escape(f['name'])}\b", scopes)
            or re.search(rf"def {re.escape(f['name'])}\(", changed)]


HUNK = re.compile(r"^@@ -(\d+)")


def changed_lines(patch: str) -> set:
    """Base-branch line numbers a diff touches; an inserted line counts as the line just above it."""
    out, old = set(), 0
    for l in (patch or "").splitlines():
        if m := HUNK.match(l):
            old = int(m[1])
        elif l.startswith("-"):
            out.add(old)
            old += 1
        elif l.startswith("+"):
            out.add(max(old - 1, 1))
        elif not l.startswith("\\"):
            old += 1
    return out


def mentioned(text: str, funcs, files):
    """Functions/files an issue talks about: `name`, name(), or a file path/basename."""
    hits = [f["qname"] for f in funcs if len(f["name"]) > 3 and not f["name"].startswith("__")
            and re.search(rf"(`{re.escape(f['name'])}`|\b{re.escape(f['name'])}\()", text)]
    hits += [f["path"] for f in files if f["path"] in text or re.search(rf"\b{re.escape(f['path'].rsplit('/', 1)[-1])}\b", text)]
    return hits


# ---------- write ----------

def write(g, cypher, rows, size=500):
    for i in range(0, len(rows), size):
        g.query(cypher, {"rows": rows[i:i + size]})


def ingest(repo: str, log=print):
    g = graph(repo)
    for q in SCHEMA:
        try:
            g.query(q)
        except Exception:  # index already exists
            pass
    now = datetime.now(timezone.utc).isoformat()
    g.query("MERGE (r:Repo {name: $repo}) SET r.status = 'ingesting', r.started_at = $now", {"repo": repo, "now": now})

    with gh() as c:
        meta = c.get(f"/repos/{repo}").raise_for_status().json()
        # a fork has no PR history of its own, so it learns from its upstream's
        hist = meta["parent"]["full_name"] if meta.get("fork") else repo
        owner, name = hist.split("/")
        log(f"downloading {repo}@{meta['default_branch']}")
        tar = c.get(f"/repos/{repo}/tarball/{meta['default_branch']}").raise_for_status().content
        if len(tar) > MAX_TARBALL:
            raise ValueError("repo too large")
        files, funcs, calls, imports = parse_code(tar)
        file_imports, call_edges = resolve(files, funcs, calls, imports)
        log(f"code: {len(files)} files, {len(funcs)} functions, {len(call_edges)} calls, {len(file_imports)} imports")

        # Code is rebuilt on every sync; history and memory nodes stay, so facts can go stale.
        g.query("MATCH ()-[e:CALLS|IMPORTS|DEFINES]->() DELETE e")
        g.query("MATCH (f:Function) WHERE NOT f.qname IN $live DETACH DELETE f", {"live": [f["qname"] for f in funcs]})
        write(g, "UNWIND $rows AS r MERGE (f:File {path: r.path}) SET f.loc = r.loc, f.test = r.test, f.hash = r.hash", files)
        write(g, """UNWIND $rows AS r MATCH (file:File {path: r.path}) MERGE (f:Function {qname: r.qname})
                    SET f.name = r.name, f.path = r.path, f.line = r.line, f.end_line = r.end_line, f.doc = r.doc, f.hash = r.hash
                    MERGE (file)-[:DEFINES]->(f)""", funcs)
        write(g, "UNWIND $rows AS r MATCH (a:Function {qname: r[0]}), (b:Function {qname: r[1]}) CREATE (a)-[:CALLS]->(b)", call_edges)
        write(g, "UNWIND $rows AS r MATCH (a:File {path: r[0]}), (b:File {path: r[1]}) CREATE (a)-[:IMPORTS]->(b)", file_imports)

        prs, cursor = [], None
        while len(prs) < MAX_PRS:
            page = graphql(c, PRS_QUERY, owner=owner, name=name, cursor=cursor)["pullRequests"]
            prs += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        prs = prs[:MAX_PRS]
        log(f"history from {hist}: {len(prs)} merged PRs, fetching diffs")

        def pr_files(pr):
            r = c.get(f"/repos/{hist}/pulls/{pr['number']}/files", params={"per_page": 100})
            return r.json() if r.is_success else []

        with ThreadPoolExecutor(8) as pool:
            diffs = list(pool.map(pr_files, prs))
        open_issues = graphql(c, ISSUES_QUERY, owner=owner, name=name)["issues"]["nodes"]

    funcs_by_path = defaultdict(list)
    for f in funcs:
        funcs_by_path[f["path"]].append(f)
    write(g, """UNWIND $rows AS r MERGE (p:PR {number: r.number})
                SET p.title = r.title, p.body = r.body, p.url = r.url, p.merged_at = r.merged_at
                MERGE (a:Person {login: r.author}) MERGE (a)-[:AUTHORED]->(p)""",
          [{"number": p["number"], "title": p["title"], "body": (p["body"] or "")[:3000], "url": p["url"],
            "merged_at": p["mergedAt"], "author": login(p["author"])} for p in prs])
    write(g, "UNWIND $rows AS r MATCH (p:PR {number: r.pr}) MERGE (u:Person {login: r.login}) MERGE (u)-[:REVIEWED]->(p)",
          [{"pr": p["number"], "login": l} for p in prs
           for l in {login(rv["author"]) for rv in p["reviews"]["nodes"]} - {login(p["author"])}])
    touched = [{"pr": p["number"], "path": f["filename"]} for p, fs in zip(prs, diffs) for f in fs]
    write(g, "UNWIND $rows AS r MATCH (p:PR {number: r.pr}), (f:File {path: r.path}) MERGE (p)-[:TOUCHED]->(f)", touched)
    modified = [{"pr": p["number"], "qname": q} for p, fs in zip(prs, diffs) for f in fs
                for q in changed_functions(f.get("patch"), funcs_by_path[f["filename"]])]
    write(g, "UNWIND $rows AS r MATCH (p:PR {number: r.pr}), (f:Function {qname: r.qname}) MERGE (p)-[:MODIFIED]->(f)", modified)

    fixed = [(p["number"], issue_row(i)) for p in prs for i in p["closingIssuesReferences"]["nodes"]]
    issues = [i for _, i in fixed] + [issue_row(i) for i in open_issues]
    write(g, """UNWIND $rows AS r MERGE (i:Issue {number: r.number})
                SET i.title = r.title, i.body = r.body, i.url = r.url, i.state = r.state, i.created_at = r.created_at
                MERGE (u:Person {login: r.author}) MERGE (u)-[:OPENED]->(i)""", issues)
    write(g, "UNWIND $rows AS r MATCH (p:PR {number: r.pr}), (i:Issue {number: r.issue}) MERGE (p)-[:FIXES]->(i)",
          [{"pr": n, "issue": i["number"]} for n, i in fixed])
    mentions = [{"issue": i["number"], "target": t} for i in issues if i["state"] == "OPEN"
                for t in mentioned(f"{i['title']}\n{i['body']}", funcs, files)]
    write(g, """UNWIND $rows AS r MATCH (i:Issue {number: r.issue}), (t)
                WHERE (t:Function AND t.qname = r.target) OR (t:File AND t.path = r.target)
                MERGE (i)-[:MENTIONS]->(t)""", mentions)

    g.query("MATCH (r:Repo {name: $repo}) SET r.status = 'ready', r.synced_at = $now, r.branch = $branch, r.description = $desc, r.history_from = $hist",
            {"repo": repo, "now": datetime.now(timezone.utc).isoformat(), "branch": meta["default_branch"], "desc": meta.get("description") or "", "hist": hist})
    stats = {"files": len(files), "functions": len(funcs), "calls": len(call_edges), "prs": len(prs),
             "issues": len(issues), "modified_links": len(modified), "issue_mentions": len(mentions)}
    log(f"done: {stats}")
    return stats


if __name__ == "__main__":
    ingest(sys.argv[1])
