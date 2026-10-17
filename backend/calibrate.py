
import sys
import tools as T

T.THRESH = 0.0
print(T.build_index())
inn, out = [], []
for line in open(sys.argv[1], encoding="utf-8"):
    kind, _, q = line.strip().partition(":")
    if kind not in ("in", "out") or not q.strip():
        continue
    top = max(T.search_metadata(q.strip())["best"], 0)
    #top = max(T.search_metadata(q.strip(), k=1)["hits"][0]["score"], 0) if True else 0
    (inn if kind == "in" else out).append(top)
    print(f"{kind:4}{top:6.3f}  {q.strip()}")
if inn and out:
    lo, hi = min(inn), max(out)
    print(f"\nlowest in-scope: {lo:.3f}   highest out-of-scope: {hi:.3f}")
    print(f"suggested SIM_THRESHOLD={(lo + hi) / 2:.2f}" if lo > hi else
          f"groups overlap (in-scope min {lo:.2f} <= out-of-scope max {hi:.2f}): improve descriptions in meta_columns/meta_tables, then rerun")
