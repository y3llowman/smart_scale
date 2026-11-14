"""
Дообучение и сравнение EfficientNet на нашем датасете (запускать на ПК с RTX 5060).
    python train.py b0 b1 b2 b3 b4          # сравнить: каждую модель одним рецептом, 15 эпох
    python train.py b2 --epochs 40          # финальное дообучение выбранной модели
Данные: custom_dataset/dataset/labels.csv (build_dataset.py), train/test по исходному фото (DATA_PIPELINE.md, раздел 7).
Дополнительный тест — настоящие кадры ESP32-CAM из custom_dataset/esp32/<класс>/ (их нет в обучении).
Результат: runs/<модель>/best.pt, model.onnx, classes.json; строка в runs/results.csv.
Скорость на сервере — отдельно: export_onnx.py + bench_latency.py.
"""
import argparse
import csv
import json
import time
from pathlib import Path

import pandas as pd
import timm
import torch
from PIL import Image
from sklearn.model_selection import StratifiedGroupKFold
from timm.data import create_transform, resolve_data_config
from torch.utils.data import DataLoader, Dataset

DATA = Path("custom_dataset")
RUNS = Path("runs")


class Images(Dataset):
    def __init__(self, paths, labels, transform):
        self.paths, self.labels, self.transform = list(paths), list(labels), transform

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, i):
        return self.transform(Image.open(self.paths[i]).convert("RGB")), self.labels[i]


def load_splits():
    imgs = pd.read_csv(DATA / "dataset" / "labels.csv", dtype=str).drop_duplicates("file")
    tr, te = next(StratifiedGroupKFold(n_splits=7, shuffle=True, random_state=0)
                  .split(imgs, imgs["class"], groups=imgs["source"]))
    classes = sorted(imgs["class"].unique())
    idx = {c: i for i, c in enumerate(classes)}
    split = lambda df: ([DATA / "dataset" / f for f in df["file"]], [idx[c] for c in df["class"]])

    # Настоящие кадры ESP32-CAM — только классов, которые есть в обучении
    esp = [(p, idx[p.parent.name]) for p in sorted((DATA / "esp32").glob("*/*.jpg")) if p.parent.name in idx]
    skipped = {p.parent.name for p in (DATA / "esp32").glob("*/*.jpg")} - set(idx)
    if skipped:
        print(f"esp32: классов нет в обучении, пропущены: {sorted(skipped)}")
    return classes, split(imgs.iloc[tr]), split(imgs.iloc[te]), ([p for p, _ in esp], [y for _, y in esp])


@torch.no_grad()
def evaluate(model, loader, device):
    model.eval()
    top1 = top3 = n = 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        k = model(x).topk(min(3, model.num_classes), dim=1).indices
        top1 += (k[:, 0] == y).sum().item()
        top3 += (k == y[:, None]).any(1).sum().item()
        n += len(y)
    return (top1 / n, top3 / n) if n else (float("nan"), float("nan"))


def run(name, epochs, classes, train, val, esp, device):
    model = timm.create_model(name, pretrained=True, num_classes=len(classes)).to(device)
    cfg = resolve_data_config({}, model=model)
    size = cfg["input_size"][-1]
    workers = 4 if device.type == "cuda" else 0
    load = lambda data, tf, shuffle: DataLoader(Images(*data, tf), batch_size=32, shuffle=shuffle,
                                                num_workers=workers, pin_memory=device.type == "cuda")
    # Еда снимается сверху: поворот/отражение не меняют класс. Цвет почти не трогаем — он важен для еды
    train_dl = load(train, create_transform(**cfg, is_training=True, hflip=0.5, vflip=0.5,
                                            color_jitter=0.1, scale=(0.6, 1.0)), True)
    val_dl = load(val, create_transform(**cfg), False)
    esp_dl = load(esp, create_transform(**cfg), False)

    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=0.05)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-4, total_steps=epochs * len(train_dl), pct_start=0.1)
    loss_fn = torch.nn.CrossEntropyLoss(label_smoothing=0.1)
    use_amp = device.type == "cuda"
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)
    best, t0 = (-1.0, -1.0), time.time()

    for epoch in range(1, epochs + 1):
        model.train()
        for x, y in train_dl:
            x, y = x.to(device), y.to(device)
            with torch.autocast(device.type, enabled=use_amp):
                loss = loss_fn(model(x), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
        v1, v3 = evaluate(model, val_dl, device)
        print(f"{name} epoch {epoch:2d}/{epochs}  loss {loss.item():.3f}  val top1 {v1:.3f} top3 {v3:.3f}", flush=True)
        if (v3, v1) > best:  # гейт проекта — топ-3, при равенстве смотрим топ-1
            best = (v3, v1)
            torch.save(model.state_dict(), out / "best.pt")

    model.load_state_dict(torch.load(out / "best.pt", map_location=device))
    e1, e3 = evaluate(model, esp_dl, device)
    model.cpu().eval()
    torch.onnx.export(model, torch.randn(1, 3, size, size), out / "model.onnx", input_names=["input"],
                      output_names=["logits"], opset_version=17, dynamo=False)
    (out / "classes.json").write_text(json.dumps({"classes": classes, "data_config": cfg}, ensure_ascii=False, indent=2))
    return {"model": name, "px": size, "epochs": epochs, "val_top1": round(best[1], 3), "val_top3": round(best[0], 3),
            "esp32_top1": round(e1, 3), "esp32_top3": round(e3, 3), "n_train": len(train[0]), "n_val": len(val[0]),
            "n_esp32": len(esp[0]), "minutes": round((time.time() - t0) / 60, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="b0 b1 b2 b3 b4")
    ap.add_argument("--epochs", type=int, default=15)
    args = ap.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    classes, train, val, esp = load_splits()
    print(f"device {device}; {len(classes)} классов; train {len(train[0])}, val {len(val[0])}, esp32 {len(esp[0])}")

    results = [run(f"efficientnet_{m}", args.epochs, classes, train, val, esp, device) for m in args.models]
    path = RUNS / "results.csv"
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=results[0].keys())
        if new:
            w.writeheader()
        w.writerows(results)
    print("\n" + pd.DataFrame(results).to_string(index=False))


if __name__ == "__main__":
    main()
