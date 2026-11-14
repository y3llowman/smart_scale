"""
Скорость EfficientNet B0–B4 (ONNX Runtime, CPU) — как на маломощном сервере: 1 поток = 1 vCPU.
    python bench_latency.py            # 1 поток
    python bench_latency.py 2          # 2 потока (сервер с 2 vCPU)
Нужны только onnxruntime и numpy: папку onnx_models/ и этот файл можно скопировать на VPS и запустить там.
Бюджет из CLAUDE.md: ≤150–200 мс на вызов модели на 1 vCPU.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODELS_DIR = Path(__file__).parent / "onnx_models"
WARMUP, RUNS = 5, 30


def bench(path: Path, size: int, threads: int):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    x = np.random.rand(1, 3, size, size).astype(np.float32)
    for _ in range(WARMUP):
        sess.run(None, {"input": x})
    times = []
    for _ in range(RUNS):
        t = time.perf_counter()
        sess.run(None, {"input": x})
        times.append((time.perf_counter() - t) * 1000)
    return np.percentile(times, 50), np.percentile(times, 95)


def main():
    threads = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    info = json.loads((MODELS_DIR / "models.json").read_text())
    print(f"CPU threads: {threads}   onnxruntime {ort.__version__}\n")
    print(f"{'model':16s} {'px':>4s} {'params':>7s}   {'FP32 p50/p95 ms':>16s}   {'INT8 p50/p95 ms':>16s}")
    for name, m in info.items():
        row = f"{name:16s} {m['size']:4d} {m['params_m']:6.1f}M"
        for kind in ("fp32", "int8"):
            p50, p95 = bench(MODELS_DIR / f"{name}_{kind}.onnx", m["size"], threads)
            row += f"   {p50:7.0f} / {p95:6.0f}"
        print(row, flush=True)


if __name__ == "__main__":
    main()
