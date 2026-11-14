"""Деградация фото с iPhone до качества камеры OV2640 на ESP32-CAM.

Что имитируется (параметры случайные в разумных пределах, чтобы модель
не переобучилась на один фиксированный «фильтр»):
  * дешёвый объектив с фиксированным фокусом: размытие, виньетирование,
    лёгкая бочкообразная дисторсия;
  * маленькая матрица: шум, зависящий от яркости, цветовой шум,
    узкий динамический диапазон (пересвет и «заваленные» тени);
  * ошибки баланса белого и бледные цвета;
  * разрешение SVGA/XGA/UXGA и сильное JPEG-сжатие (иногда двойное).

degrade()  — испорченная копия + параметры (для params.json);
map_box()  — рамка с оригинала на копию по этим параметрам (та же геометрия: кроп 4:3, масштаб, дисторсия).
Запускается из build_dataset.py.
"""

import random

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:  # фото с iPhone по умолчанию в HEIC
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
RESOLUTIONS = [(800, 600), (1024, 768), (1600, 1200)]  # SVGA, XGA, UXGA


def crop_box_4x3(w: int, h: int) -> tuple[int, int, int, int]:
    if w / h > 4 / 3:
        nw = int(h * 4 / 3)
        return (w - nw) // 2, 0, (w - nw) // 2 + nw, h
    nh = int(w * 3 / 4)
    return 0, (h - nh) // 2, w, (h - nh) // 2 + nh


def barrel(arr: np.ndarray, k: float) -> np.ndarray:
    """Лёгкая бочкообразная дисторсия (ближайший сосед — быстро и без OpenCV)."""
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    x, y = (xx - cx) / cx, (yy - cy) / cy
    r2 = x * x + y * y
    f = 1 + k * r2
    xs = np.clip(x * f * cx + cx, 0, w - 1).astype(np.int32)
    ys = np.clip(y * f * cy + cy, 0, h - 1).astype(np.int32)
    return arr[ys, xs]


def degrade(img: Image.Image, rng: random.Random) -> tuple[Image.Image, dict]:
    p = {
        "res": rng.choice(RESOLUTIONS),
        "blur": rng.uniform(0.4, 1.3),
        "distortion": rng.uniform(0.0, 0.08),
        "wb_r": rng.uniform(0.88, 1.12),
        "wb_b": rng.uniform(0.88, 1.12),
        "saturation": rng.uniform(0.70, 0.95),
        "exposure": rng.uniform(0.75, 1.25),
        "contrast": rng.uniform(0.80, 1.0),
        "vignette": rng.uniform(0.10, 0.35),
        "noise_shot": rng.uniform(0.002, 0.012),
        "noise_read": rng.uniform(0.004, 0.015),
        "chroma_noise": rng.uniform(0.005, 0.02),
        "jpeg_q": rng.randint(35, 75),
        "double_jpeg": rng.random() < 0.3,
    }
    img = ImageOps.exif_transpose(img).convert("RGB")
    p["crop"] = crop_box_4x3(*img.size)
    img = img.crop(p["crop"])

    # 1) «оптика»: уменьшаем до размера матрицы 2 Мп и размываем
    img = img.resize((1600, 1200), Image.LANCZOS).filter(ImageFilter.GaussianBlur(p["blur"]))
    a = np.asarray(img).astype(np.float32) / 255.0
    if p["distortion"] > 0.01:
        a = barrel(a, p["distortion"])

    # 2) баланс белого, экспозиция, бледность, узкий динамический диапазон
    a[..., 0] *= p["wb_r"]
    a[..., 2] *= p["wb_b"]
    a *= p["exposure"]
    gray = a.mean(axis=2, keepdims=True)
    a = gray + (a - gray) * p["saturation"]
    a = 0.5 + (a - 0.5) * p["contrast"]
    a = np.clip(a, 0, 1)

    # 3) виньетирование
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / np.sqrt(2)
    a *= (1 - p["vignette"] * r ** 2)[..., None]

    # 4) шум матрицы: дробовой (растёт с яркостью) + шум считывания + цветовые пятна
    nrng = np.random.default_rng(rng.randrange(2**32))
    sigma = np.sqrt(p["noise_shot"] * a + p["noise_read"] ** 2)
    a = a + nrng.normal(0, 1, a.shape) * sigma
    small = nrng.normal(0, p["chroma_noise"], (h // 8, w // 8, 3)).astype(np.float32)
    chroma = np.asarray(
        Image.fromarray(((small + 0.5) * 255).clip(0, 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    ).astype(np.float32) / 255.0 - 0.5
    a = np.clip(a + chroma, 0, 1)

    img = Image.fromarray((a * 255).astype(np.uint8)).resize(p["res"], Image.BILINEAR)

    # 5) JPEG с субдискретизацией 4:2:0, иногда двойное сжатие (как при пересохранении на сервере)
    from io import BytesIO

    for q in ([p["jpeg_q"], rng.randint(60, 85)] if p["double_jpeg"] else [p["jpeg_q"]]):
        buf = BytesIO()
        img.save(buf, "JPEG", quality=q, subsampling=2)
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
    return img, p


def map_box(box: list[float], p: dict) -> list[int] | None:
    """Рамка [x1, y1, x2, y2] на оригинале (после поворота по EXIF) → рамка на копии с параметрами p.
    Края рамки после дисторсии выгибаются, поэтому переносим точки по всему контуру, а не 4 угла.
    None — объект целиком вне кадра копии (срезан кропом 4:3)."""
    x1, y1, x2, y2 = box
    t = np.linspace(0, 1, 9)
    x = np.concatenate([x1 + (x2 - x1) * t, np.full(9, x2), x1 + (x2 - x1) * t, np.full(9, x1)])
    y = np.concatenate([np.full(9, y1), y1 + (y2 - y1) * t, np.full(9, y2), y1 + (y2 - y1) * t])
    cx0, cy0, cx1, cy1 = p["crop"]
    x = (x - cx0) * 1600 / (cx1 - cx0)
    y = (y - cy0) * 1200 / (cy1 - cy0)
    if p["distortion"] > 0.01:  # barrel() берёт пиксель копии из точки r·(1 + k·r²) — обратное ищем итерациями
        cx, cy = 1599 / 2, 1199 / 2
        sx, sy = (x - cx) / cx, (y - cy) / cy
        ox, oy = sx, sy
        for _ in range(20):
            f = 1 + p["distortion"] * (ox**2 + oy**2)
            ox, oy = sx / f, sy / f
        x, y = ox * cx + cx, oy * cy + cy
    w, h = p["res"]
    x, y = np.clip(x * w / 1600, 0, w), np.clip(y * h / 1200, 0, h)
    if x.max() - x.min() < 2 or y.max() - y.min() < 2:
        return None
    return [round(float(v)) for v in (x.min(), y.min(), x.max(), y.max())]
