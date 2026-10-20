"""Сборка датасета: фото по папкам классов → испорченные копии (качество OV2640) + labels.csv.

Запуск из папки smart_scale:  python build_dataset.py

custom_dataset/
  raw/<класс>/IMG_xxx.HEIC|JPG  ВЫ: фото по классам (класс = имя папки). Скрипт их не меняет.
  cvat/verified/*.zip           ВЫ: экспорт задач из CVAT («CVAT for images 1.1»), zip как скачался
  cvat/upload.zip               → в CVAT: фото штрихкодов, ещё не проверенные человеком
  cvat/prelabels.xml            → в CVAT: авто-рамки и значения zxing для них (импорт «CVAT 1.1»)
  dataset/images/<класс>/IMG_xxx_ov0.jpg   испорченные копии
  dataset/labels.csv            строка = объект; у фото еды одна строка без рамки
  dataset/params.json           параметры порчи каждой копии

labels.csv:
  file      копия (относительно dataset/)      source   оригинал — группа для train/test split
  class     имя папки                          barcode  значение кода ('' — не виден или не прочитан)
  x1..y2    рамка кода на копии, px            readable zxing прочитал на копии ровно это значение
  checked   1 — разметка этого фото проверена в CVAT, 0 — только автомат
Штрихкоды размечаются только в классе barcodes: значение и рамку даёт zxing-cpp на оригинале
(значение с контрольной цифрой — точное), всё фото проходит проверку в CVAT один раз.
Повторный запуск портит только новые фото; labels.csv и файлы для CVAT пересобираются целиком.
"""

import csv
import io
import json
import random
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import zxingcpp
from PIL import Image, ImageOps

from ov2640_sim import EXTS, degrade, map_box

ROOT = Path("custom_dataset")
RAW, CVAT, OUT = ROOT / "raw", ROOT / "cvat", ROOT / "dataset"
BARCODE_CLASS = "barcodes"
VARIANTS = 3
COLUMNS = ["file", "source", "class", "barcode", "x1", "y1", "x2", "y2", "readable", "checked"]


def zxing_objects(img: Image.Image) -> list[dict]:
    """Коды, прочитанные zxing. У линейных кодов zxing отдаёт полоску по строкам, где прочитал, —
    дотягиваем её поперёк кода до половины длины (низ типичной высоты штрихов EAN); точно — в CVAT."""
    out = []
    for r in zxingcpp.read_barcodes(img):
        p = r.position
        q = np.array([[p.top_left.x, p.top_left.y], [p.top_right.x, p.top_right.y],
                      [p.bottom_right.x, p.bottom_right.y], [p.bottom_left.x, p.bottom_left.y]], float)
        along, length = q[1] - q[0], np.linalg.norm(q[1] - q[0])
        across = np.array([-along[1], along[0]]) / max(length, 1)
        height = max(np.linalg.norm(q[3] - q[0]), 0.5 * length)
        q = q.mean(0) + np.array([s * along / 2 + t * across * height / 2 for s in (-1, 1) for t in (-1, 1)])
        x1, y1 = q.min(0).clip(0)
        x2, y2 = q.max(0).clip(None, img.size)
        out.append({"value": r.text, "box": [float(v) for v in (x1, y1, x2, y2)]})
    return out


def read_verified() -> dict[str, list[dict]]:
    """Экспорты CVAT: {имя фото без расширения: коды}. Фото без рамок — «код не виден», тоже проверено."""
    done = {}
    for zp in sorted((CVAT / "verified").glob("*.zip")):
        with zipfile.ZipFile(zp) as z:
            root = ET.fromstring(z.read("annotations.xml"))
        for im in root.iter("image"):
            done[Path(im.get("name")).stem] = [
                {"value": (b.findtext("attribute[@name='value']") or "").strip(),
                 "box": [float(b.get(k)) for k in ("xtl", "ytl", "xbr", "ybr")]}
                for b in im.iter("box")]
    return done


def check_digit_ok(v: str) -> bool:
    """EAN/UPC-A/GTIN: сумма цифр справа налево с весами 1, 3, 1, 3… делится на 10."""
    return sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(reversed(v))) % 10 == 0


def main() -> None:
    (CVAT / "verified").mkdir(parents=True, exist_ok=True)
    log_path = OUT / "params.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {}
    verified = read_verified()
    rng = random.Random(0)
    rows = []
    xml = ET.Element("annotations")
    ET.SubElement(xml, "version").text = "1.1"
    upload = zipfile.ZipFile(CVAT / "upload.zip", "w")

    files = sorted(f for f in RAW.rglob("*") if f.suffix.lower() in EXTS)
    for i, f in enumerate(files, 1):
        cls = f.parent.name
        copies = [OUT / "images" / cls / f"{f.stem}_ov{v}.jpg" for v in range(VARIANTS)]
        is_code = cls == BARCODE_CLASS
        objs = verified.get(f.stem) if is_code else []
        img = None
        if objs is None or not all(c.exists() for c in copies):
            with Image.open(f) as im:
                img = ImageOps.exif_transpose(im).convert("RGB")
        if objs is None:  # ещё не проверено человеком: авторазметка + в очередь для CVAT
            objs = zxing_objects(img)
            name = f"{f.stem}.jpg"
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            upload.writestr(name, buf.getvalue())
            el = ET.SubElement(xml, "image", id=str(len(upload.namelist()) - 1), name=name,
                               width=str(img.width), height=str(img.height))
            for o in objs:
                b = ET.SubElement(el, "box", label="barcode", source="auto", occluded="0", z_order="0",
                                  **dict(zip(("xtl", "ytl", "xbr", "ybr"), (f"{v:.1f}" for v in o["box"]))))
                ET.SubElement(b, "attribute", name="value").text = o["value"]
        elif is_code:
            for o in objs:
                v = o["value"]
                if v.isdigit() and len(v) in (8, 12, 13, 14) and not check_digit_ok(v):
                    print(f"  ВНИМАНИЕ: {f.name}: у {v} неверная контрольная цифра — опечатка? Исправьте в CVAT")
        checked = int(f.stem in verified)

        for c in copies:
            key = c.relative_to(OUT).as_posix()
            if not c.exists():
                out, log[key] = degrade(img, rng)
                c.parent.mkdir(parents=True, exist_ok=True)
                out.save(c, "JPEG", quality=95)
            base = {"file": key, "source": f.relative_to(RAW).as_posix(), "class": cls}
            if not is_code:
                rows.append(base)
                continue
            with Image.open(c) as cm:
                read = {r.text for r in zxingcpp.read_barcodes(cm)}
            boxes = [(o["value"], map_box(o["box"], log[key])) for o in objs]
            boxes = [(v, b) for v, b in boxes if b]  # срезанные кропом 4:3 на этой копии не видны
            for v, b in boxes or [("", None)]:
                rows.append({**base, "barcode": v, **dict(zip(("x1", "y1", "x2", "y2"), b or [])),
                             "readable": int(bool(v) and v in read), "checked": checked})
        print(f"[{i}/{len(files)}] {f.relative_to(RAW)}")

    upload.close()
    todo = len(upload.namelist())
    if todo:
        ET.ElementTree(xml).write(CVAT / "prelabels.xml", encoding="utf-8", xml_declaration=True)
    else:
        (CVAT / "upload.zip").unlink()
        (CVAT / "prelabels.xml").unlink(missing_ok=True)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1))
    with open(OUT / "labels.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    code_rows = [r for r in rows if r["class"] == BARCODE_CLASS]
    print(f"\nОригиналов: {len(files)}, копий: {len(files) * VARIANTS}. "
          f"Копий штрихкодов с прочитанным кодом: {sum(r['readable'] for r in code_rows)} "
          f"из {len({r['file'] for r in code_rows})}.")
    if todo:
        print(f"Ждут проверки в CVAT: {todo} фото -> {CVAT / 'upload.zip'} + {CVAT / 'prelabels.xml'}")


if __name__ == "__main__":
    main()
