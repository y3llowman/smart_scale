"""
EfficientNet B0–B4 → ONNX FP32 + INT8 для замера скорости на сервере (bench_latency.py).
    python export_onnx.py
Веса для скорости не важны (архитектура та же), поэтому модели без предобучения — ничего не качается.
Каждая модель в своём «родном» разрешении из timm. INT8 — статическая квантизация (QDQ, per-channel),
калибровка на наших фото из custom_dataset; точность INT8 проверять отдельно, на обученной модели.
Результат: onnx_models/<model>_fp32.onnx, <model>_int8.onnx, models.json (разрешение, параметры).
"""
import json
from pathlib import Path

import numpy as np
import timm
import torch
from PIL import Image
from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static
from onnxruntime.quantization.shape_inference import quant_pre_process

MODELS = ["efficientnet_b0", "efficientnet_b1", "efficientnet_b2", "efficientnet_b3", "efficientnet_b4"]
NUM_CLASSES = 100  # примерное число наших классов; на скорость почти не влияет
OUT = Path("onnx_models")
CALIB_IMAGES = 32


class PhotoReader(CalibrationDataReader):
    """Калибровочные кадры INT8: наши фото, приведённые к входу модели (как в timm: resize + center crop)."""

    def __init__(self, size, mean, std):
        files = sorted(Path("custom_dataset").glob("*/*/*.[jJ][pP]*[gG]"))[:CALIB_IMAGES]
        self.batches = iter([{"input": self._prep(f, size, mean, std)} for f in files])

    @staticmethod
    def _prep(path, size, mean, std):
        img = Image.open(path).convert("RGB")
        scale = size / min(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.BICUBIC)
        left, top = (img.width - size) // 2, (img.height - size) // 2
        x = np.asarray(img.crop((left, top, left + size, top + size)), np.float32) / 255
        x = (x - mean) / std
        return x.transpose(2, 0, 1)[None].astype(np.float32)

    def get_next(self):
        return next(self.batches, None)


def main():
    OUT.mkdir(exist_ok=True)
    info = {}
    for name in MODELS:
        model = timm.create_model(name, pretrained=False, num_classes=NUM_CLASSES).eval()
        cfg = model.pretrained_cfg
        size = cfg["input_size"][-1]
        fp32, pre, int8 = OUT / f"{name}_fp32.onnx", OUT / f"{name}_pre.onnx", OUT / f"{name}_int8.onnx"

        torch.onnx.export(model, torch.randn(1, 3, size, size), fp32, input_names=["input"],
                          output_names=["logits"], opset_version=17, dynamo=False)
        quant_pre_process(str(fp32), str(pre))
        quantize_static(str(pre), str(int8),
                        PhotoReader(size, np.array(cfg["mean"], np.float32), np.array(cfg["std"], np.float32)),
                        quant_format=QuantFormat.QDQ, per_channel=True,
                        activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8)
        pre.unlink()

        info[name] = {"size": size, "params_m": round(sum(p.numel() for p in model.parameters()) / 1e6, 1)}
        print(f"{name}: {size}px, {info[name]['params_m']}M params, "
              f"fp32 {fp32.stat().st_size / 2**20:.0f} MB, int8 {int8.stat().st_size / 2**20:.0f} MB", flush=True)
    (OUT / "models.json").write_text(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
