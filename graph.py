"""FalkorDB connection, plus turning query results into text (for the LLM) and nodes/edges (for the UI)."""
import atexit
import io
import os
import platform
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path as FilePath
from urllib.parse import urlparse

import httpx
import zstandard
from falkordb import Edge, FalkorDB, Node, Path
from redis.exceptions import ConnectionError as DBDown

# Embedded fallback: Redis 8 + its libs from conda-forge (built for glibc 2.17, so it runs on Render's native runtime,
# unlike falkordblite's own redis-server), plus the FalkorDB module from the falkordblite wheel.
CONDA = "https://conda.anaconda.org/conda-forge/linux-64/"
EMBED_PKGS = [CONDA + p for p in ("redis-server-8.6.2-h9a70dda_0.conda", "libstdcxx-16.2.0-h934c35e_7.conda",
                                  "libgcc-16.2.0-ha9f2e26_7.conda", "libjemalloc-5.3.0-h5888daf_1.conda",
                                  "openssl-3.5.9-h781a0a9_0.conda")]
FALKORDBLITE = ("https://files.pythonhosted.org/packages/8a/5e/d343c5249bd24c6614e8c1b718a73bb9f68beb2cf90464bc51c9436d25af/"
                "falkordblite-0.10.0-cp312-cp312-manylinux_2_39_x86_64.whl")
SNAPSHOT = FilePath(__file__).with_name("snapshot.rdb")
_db, _lock = None, threading.Lock()


def db():
    global _db
    with _lock:
        if _db is None:
            _db = _connect()
    return _db


def _connect():
    # Not FalkorDB.from_url: with redis-py 8 it opens a RESP3 pool that drops replies mid-read.
    u = urlparse(os.environ.get("FALKORDB_URL", "falkor://localhost:6379"))
    try:  # the constructor already connects
        return FalkorDB(host=u.hostname, port=u.port or 6379, username=u.username or None, password=u.password,
                        ssl=u.scheme == "falkors", socket_connect_timeout=5)
    except DBDown as e:
        if sys.platform != "linux":
            raise
        print(f"FalkorDB at {u.hostname} unreachable ({e}), starting embedded FalkorDB from {SNAPSHOT.name}", flush=True)
        return FalkorDB(host="127.0.0.1", port=embedded())


def embedded(port=6390, root=FilePath("/tmp/falkordb")):
    """Run FalkorDB next to the app, preloaded with snapshot.rdb. What's written at runtime lasts until restart."""
    server, module = root / "bin/redis-server", root / "redislite/bin/falkordb.so"
    if not module.exists():
        for url in EMBED_PKGS:  # .conda = zip holding pkg-*.tar.zst
            z = zipfile.ZipFile(io.BytesIO(httpx.get(url, follow_redirects=True, timeout=120).content))
            pkg = next(n for n in z.namelist() if n.startswith("pkg-"))
            with tarfile.open(fileobj=zstandard.ZstdDecompressor().stream_reader(io.BytesIO(z.read(pkg))), mode="r|") as t:
                t.extractall(root, filter="tar")
        whl = zipfile.ZipFile(io.BytesIO(httpx.get(FALKORDBLITE, timeout=120).content))
        for n in whl.namelist():
            if n == "redislite/bin/falkordb.so" or n.startswith("falkordblite.libs/"):
                whl.extract(n, root)
        module.chmod(0o755)
    log = root / "redis.log"
    proc = subprocess.Popen([server, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--dir", SNAPSHOT.parent,
                             "--dbfilename", SNAPSHOT.name, "--loadmodule", module, "THREAD_COUNT", "2",
                             "OMP_THREAD_COUNT", "1"], stdout=log.open("w"), stderr=subprocess.STDOUT)
    atexit.register(proc.terminate)
    for _ in range(120):
        if proc.poll() is not None:
            raise DBDown(f"embedded FalkorDB exited ({platform.libc_ver()}): {log.read_text()[-800:]}")
        try:
            FalkorDB(host="127.0.0.1", port=port).connection.ping()  # BusyLoadingError while the snapshot loads
            return port
        except DBDown:
            time.sleep(0.5)
    raise DBDown("embedded FalkorDB did not start in 60s")


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
