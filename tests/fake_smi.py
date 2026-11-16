"""nvidia-smi simulado. FAKE_GPUS=N (def. 2), FAKE_NVLINK=pairs|all|none (def. pairs)."""
import os, sys

N = int(os.environ.get("FAKE_GPUS", "2"))
MODE = os.environ.get("FAKE_NVLINK", "pairs")
a = sys.argv[1:]


def edge(i, j):
    if i == j:
        return "X"
    if MODE == "all":
        return "NV12"
    if MODE == "pairs" and i // 2 == j // 2:
        return "NV4"
    return "SYS" if abs(i - j) > 3 else "PHB"


if any(x.startswith("--query-gpu") for x in a):
    ext = any("pcie" in x for x in a)
    for i in range(N):
        util = 87 if i == 0 else (12 * i) % 100
        row = "%d, NVIDIA GeForce RTX 3090, %d, %d, 24576, %d, %.1f, 350.0, 560.35" % (i, util, 8000 + 500 * i, 50 + 3 * i, 140.5 + 10 * i)
        print(row + (", 4, 16" if ext else ""))
elif a[:1] == ["-L"]:
    for i in range(N):
        print("GPU %d: NVIDIA GeForce RTX 3090 (UUID: GPU-%08d)" % (i, i))
elif a[:1] == ["topo"]:
    print("\t" + "\t".join("GPU%d" % i for i in range(N)) + "\tCPU Affinity\tNUMA Affinity")
    for i in range(N):
        print("GPU%d\t" % i + "\t".join(edge(i, j) for j in range(N)) + "\t0-23\t0")
    print("\nLegend:\n\n  X    = Self\n  SYS  = Connection traversing PCIe as well as the SMP interconnect\n  NV#  = Connection traversing a bonded set of # NVLinks")
elif a[:1] == ["nvlink"]:
    for i in range(N):
        print("GPU %d: NVIDIA GeForce RTX 3090 (UUID: GPU-%08d)" % (i, i))
        for k in range(4):
            has = any(edge(i, j).startswith("NV") for j in range(N) if j != i)
            print("\t Link %d: %s" % (k, "14.062 GB/s" if has else "<inactive>"))
else:
    print("| NVIDIA-SMI 560.35   Driver Version: 560.35   CUDA Version: 12.6 |")
