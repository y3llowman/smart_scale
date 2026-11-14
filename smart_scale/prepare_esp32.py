"""Подготовка реальных кадров ESP32-CAM к обучению: фото в custom_dataset/esp32/<класс>/ + экспорт Label Studio.

Запуск из папки smart_scale:
    python prepare_esp32.py                  # проверить json, собрать labels.csv с фолдами
    python prepare_esp32.py --overlay qa.jpg # плюс контактный лист с нарисованными эллипсами

Что делает:
  - сопоставляет задачи Label Studio с файлами на диске (в экспорте имя имеет вид
    "<8 hex>-esp_....jpg", путь — "/data/upload/3/..."); файлы не трогает;
  - пишет custom_dataset/esp32/labels.csv: file, source, class, session, annotated,
    n_eggs, cx, cy, rx, ry, rot, fold;
  - печатает пропавшие файлы, лишние задачи и кадры без разметки.

Класс берётся из имени папки — для классификатора разметка не нужна, эллипсы нужны
только чтобы нарезать объекты/обучить локализатор. Геометрия эллипса хранится в пикселях
(в LS это проценты ширины/высоты кадра), поворот — градусы против часовой стрелки.
"""
import argparse
import csv
import json
import math
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw

ESP = Path("custom_dataset/esp32")
COLUMNS = ["file", "source", "class", "session", "annotated", "n_eggs", "cx", "cy", "rx", "ry", "rot", "fold"]
FOLDS = 5


def strip_prefix(name: str) -> str:
    """Label Studio: f484eccb-esp_20261031_184034_401.jpg → esp_20261031_184034_401.jpg."""
    return re.sub(r"^[0-9a-f]{8}-", "", name)


def find_json() -> Path:
    found = sorted(ESP.glob("*.json"))
    if len(found) != 1:
        raise SystemExit(f"ожидался один json в {ESP}, найдено: {[p.name for p in found]}")
    return found[0]


def ellipse_px(result: dict, w: int, h: int) -> tuple[float, float, float, float, float]:
    v = result["value"]
    return (v["x"] / 100 * w, v["y"] / 100 * h, v["radiusX"] / 100 * w,
            v["radiusY"] / 100 * h, float(v.get("rotation", 0.0)))


def ellipse_points(cx, cy, rx, ry, rot_deg, n=120):
    a = math.radians(rot_deg)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a)))
    return pts


def build_rows(tasks: list) -> list[dict]:
    """Класс берём из папки, поэтому в manifest попадают все кадры, а не только размеченные."""
    by_file = {}
    for p in sorted(ESP.glob("*/*.jpg")):
        by_file[p.name] = {"file": p.name, "source": p.stem, "class": p.parent.name,
                           "session": p.stem.split("_")[1] if "_" in p.stem else "", "annotated": 0, "n_eggs": 0,
                           "cx": "", "cy": "", "rx": "", "ry": "", "rot": ""}

    for t in tasks:
        name = strip_prefix(t["file_upload"])
        if name not in by_file:
            print(f"в json есть, на диске нет: {name}")
            continue
        row = by_file[name]
        w, h = Image.open(ESP / row["class"] / name).size
        eggs = [r for a in t["annotations"] for r in a["result"] if r["type"] == "ellipselabels"]
        row["annotated"], row["n_eggs"] = int(bool(eggs)), len(eggs)
        if eggs:
            *geom, rot = ellipse_px(eggs[0], w, h)
            row["cx"], row["cy"], row["rx"], row["ry"] = (round(g, 1) for g in geom)
            row["rot"] = round(rot, 1)
            if eggs[0]["value"]["ellipselabels"] != [row["class"]]:
                print(f"метка {eggs[0]['value']['ellipselabels']} в папке {row['class']}: {name}")

    rows = list(by_file.values())
    for cls in sorted({r["class"] for r in rows}):
        naked = sum(1 for r in rows if r["class"] == cls and not r["annotated"])
        if naked:
            print(f"без разметки в {cls}: {naked} (класс всё равно берётся из папки)")
    return rows


def assign_folds(rows: list[dict]) -> None:
    by_class = {}
    for r in rows:
        by_class.setdefault(r["class"], []).append(r)
    for items in by_class.values():
        random.Random(0).shuffle(items)
        for i, r in enumerate(items):
            r["fold"] = i % FOLDS


def contact_sheet(rows: list[dict], path: Path, cols: int = 4, thumb=(400, 300)) -> None:
    W, H = thumb
    rows = [r for r in rows if r["annotated"]]
    grid_h = math.ceil(len(rows) / cols) * H
    sheet = Image.new("RGB", (cols * W, grid_h), "white")
    for i, r in enumerate(rows):
        img = Image.open(ESP / r["class"] / r["file"]).convert("RGB")
        k = min(W / img.width, H / img.height)
        img = img.resize((round(img.width * k), round(img.height * k)))
        cell = ImageDraw.Draw(img)
        pts = ellipse_points(float(r["cx"]) * k, float(r["cy"]) * k, float(r["rx"]) * k, float(r["ry"]) * k,
                             float(r["rot"]))
        cell.line(pts + [pts[0]], fill=(255, 0, 0), width=3)
        cell.text((5, 5), r["file"], fill=(255, 255, 0))
        sheet.paste(img, ((i % cols) * W, (i // cols) * H))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=90)
    print(f"контактный лист: {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, help="экспорт Label Studio (по умолчанию единственный json в custom_dataset/esp32)")
    ap.add_argument("--overlay", type=Path, help="куда сохранить контактный лист с эллипсами, .jpg")
    args = ap.parse_args()

    tasks = json.loads((args.json or find_json()).read_text())
    rows = build_rows(tasks)
    assign_folds(rows)

    out = ESP / "labels.csv"
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    for cls in sorted({r["class"] for r in rows if r["class"]}):
        items = [r for r in rows if r["class"] == cls]
        print(f"{cls}: {len(items)} фото, размечено {sum(r['annotated'] for r in items)}, "
              f"эллипсов {sum(r['n_eggs'] for r in items)}")
    print(f"записано {out} ({len(rows)} строк)")
    if args.overlay:
        contact_sheet(rows, args.overlay)


if __name__ == "__main__":
    main()
