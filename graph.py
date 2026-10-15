"""FalkorDB connection, plus turning query results into text (for the LLM) and nodes/edges (for the UI)."""
import os
from functools import cache
from urllib.parse import urlparse

from falkordb import Edge, FalkorDB, Node, Path


@cache
def db():
    # Not FalkorDB.from_url: with redis-py 8 it opens a RESP3 pool that drops replies mid-read.
    u = urlparse(os.environ.get("FALKORDB_URL", "falkor://localhost:6379"))
    return FalkorDB(host=u.hostname, port=u.port or 6379, username=u.username or None, password=u.password,
                    ssl=u.scheme == "falkors")


def graph(repo: str):
    """Every repo gets its own isolated graph: 'psf/requests' -> 'psf__requests'."""
    return db().select_graph(repo.replace("/", "__"))


def repos():
    return [g.replace("__", "/", 1) for g in db().list_graphs() if "__" in g]


def name(n: Node) -> str:
    p, kind = n.properties, n.labels[0] if n.labels else "?"
    if kind in ("PR", "Issue"):
        return f"{kind} #{p.get('number')} {p.get('title', '')}"
    if kind == "Person":
        return "@" + p.get("login", "")
    return str(p.get("qname") or p.get("path") or p.get("text") or p.get("question") or p.get("name") or n.id)


class Run:
    """Collects every node/edge touched by the queries of one agent turn: that's the 'path behind the answer'."""

    def __init__(self, g):
        self.g, self.nodes, self.edges, self.log = g, {}, {}, []

    def query(self, cypher, params=None, write=False) -> str:
        self.log.append(" ".join(cypher.split()))
        res = self.g.query(cypher, params) if write else self.g.ro_query(cypher, params, timeout=10_000)
        cols = [h[1] for h in res.header]
        rows = [" | ".join(f"{c}: {self._fmt(v)}" for c, v in zip(cols, row)) for row in res.result_set]
        return "\n".join(rows) or "(no results)"

    def _fmt(self, v) -> str:
        if isinstance(v, Node):
            self.nodes[v.id] = v
            return f"{v.labels[0]}({name(v)})"
        if isinstance(v, Edge):
            self.edges[v.id] = v
            return v.relation
        if isinstance(v, Path):
            ns, out = v.nodes(), self._fmt(v.nodes()[0])
            for e, prev, nxt in zip(v.edges(), ns, ns[1:]):
                self.edges[e.id] = e
                arrow = f" -{e.relation}-> " if _id(e.src_node) == prev.id else f" <-{e.relation}- "
                out += arrow + self._fmt(nxt)
            return out
        if isinstance(v, list):
            return "[" + ", ".join(self._fmt(x) for x in v) + "]"
        if isinstance(v, float):
            return f"{v:.3f}"
        s = str(v)
        return s if len(s) < 700 else s[:700] + "..."

    def view(self):
        """vis-network friendly subgraph."""
        nodes = [{"id": n.id, "label": short(n), "group": n.labels[0], "name": name(n),
                  "props": {k: str(v)[:300] for k, v in n.properties.items() if k != "hash"}}
                 for n in self.nodes.values()]
        edges = [{"id": e.id, "from": _id(e.src_node), "to": _id(e.dest_node), "label": e.relation}
                 for e in self.edges.values() if _id(e.src_node) in self.nodes and _id(e.dest_node) in self.nodes]
        return {"nodes": nodes, "edges": edges}


def short(n: Node) -> str:
    s = name(n).split("::")[-1]
    s = s.rsplit("/", 1)[-1] if n.labels[0] == "File" else s
    return s if len(s) <= 32 else s[:30] + "..."


def _id(x):
    return getattr(x, "id", x)
