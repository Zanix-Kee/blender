import sys
if any(a.startswith("--query-gpu") for a in sys.argv):
    print("0, NVIDIA GeForce RTX 3060, 87, 8123, 12288, 63, 142.5, 170.0, 560.35")
    print("1, NVIDIA GeForce RTX 3060, 12, 900, 12288, 41, 60.0, 170.0, 560.35")
else:
    print("| NVIDIA-SMI 560.35   Driver Version: 560.35   CUDA Version: 12.6 |")
