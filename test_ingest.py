"""Checks the AST parser and name resolution on a tiny in-memory repo. Run: uv run python test_ingest.py"""
import io
import tarfile

from ingest import changed_functions, mentioned, parse_code, resolve

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from .b import helper\ndef run():\n    return helper()\nclass C:\n    def m(self):\n        return run()\n",
    "pkg/b.py": "def helper():\n    '''Does help.'''\n    return 1\n",
    "venv/lib.py": "def ignored(): pass\n",
}

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for path, src in FILES.items():
        data = src.encode()
        info = tarfile.TarInfo(f"owner-repo-abc123/{path}")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

files, funcs, calls, imports = parse_code(buf.getvalue())
assert {f["path"] for f in files} == {"pkg/__init__.py", "pkg/a.py", "pkg/b.py"}
assert {f["qname"] for f in funcs} == {"pkg/a.py::run", "pkg/a.py::C.m", "pkg/b.py::helper"}
assert next(f for f in funcs if f["name"] == "helper")["doc"] == "Does help."

file_imports, call_edges = resolve(files, funcs, calls, imports)
assert ["pkg/a.py", "pkg/b.py"] in file_imports
assert sorted(call_edges) == [["pkg/a.py::C.m", "pkg/a.py::run"], ["pkg/a.py::run", "pkg/b.py::helper"]]

patch = "@@ -1,3 +1,3 @@ def run():\n-    return helper()\n+    return helper() + 1\n"
assert changed_functions(patch, [f for f in funcs if f["path"] == "pkg/a.py"]) == ["pkg/a.py::run"]
assert mentioned("crash in `helper` from b.py", funcs, files) == ["pkg/b.py::helper", "pkg/b.py"]
print("ok")
