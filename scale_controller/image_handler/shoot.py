"""
Съёмка датасета с ESP32-CAM: живое окно, пробел — сохранить снимок 1600x1200 в папку класса.
    python shoot.py apples
Снимки → smart_scale/custom_dataset/esp32/<класс>/ (настоящие кадры OV2640, ov2640_sim их не портит).
Клавиши: пробел — снимок, l — вспышка вкл/выкл, q — выход.
Вспышка горит всю сессию: свет весов — часть «картинки», автоэкспозиция в прошивке отключена.
Настройки на лету (/control, см. комментарий у control_handler в main.cpp), клавиши «меньше/больше»:
    1/2 ae_level, 3/4 brightness, 5/6 gainceiling, 7/8 aec2,
    9/0 wb_mode, e/E aec_value, g/G agc_gain
"""
import sys
import time
from pathlib import Path

import cv2
import requests

from camera import CAPTURE_URL, STREAM_URL
from focus import control

OUT_ROOT = Path(__file__).resolve().parents[2] / "smart_scale" / "custom_dataset" / "esp32"

# Настройки: имя → [значение, минимум, максимум, шаг, клавиша «меньше», клавиша «больше»].
# Стартовые значения = фиксированные дефолты прошивки (apply_camera_defaults в main.cpp).
PARAMS = {
    "ae_level":    [0, -2, 2, 1, "1", "2"],
    "brightness":  [0, -2, 2, 1, "3", "4"],
    "gainceiling": [0, 0, 6, 1, "5", "6"],
    "aec2":        [0, 0, 1, 1, "7", "8"],
    "wb_mode":     [4, 0, 4, 1, "9", "0"],        # 0 авто, 1 солнце, 2 пасмурно, 3 офис, 4 дом
    "aec_value":   [300, 0, 1200, 20, "e", "E"],   # ручная экспозиция (exposure_ctrl=0)
    "agc_gain":    [0, 0, 30, 1, "g", "G"],        # ручное усиление (gain_ctrl=0)
}


def main():
    if len(sys.argv) != 2:
        sys.exit("Использование: python shoot.py <класс>   (например: python shoot.py apples)")
    out_dir = OUT_ROOT / sys.argv[1]
    out_dir.mkdir(parents=True, exist_ok=True)

    # стрим 640x480 для быстрого просмотра; /capture прошивка сама снимает в 1600x1200.
    # Вспышка горит всю сессию: экспозиция в прошивке фиксированная под этот свет.
    led = 1
    control("led", led)
    cap = cv2.VideoCapture(STREAM_URL)
    msg, msg_until = "", 0.0
    try:
        while True:
            ok, frame = cap.read()
            frame = cv2.resize(frame, (720, 560))
            if not ok:
                cap.release()
                cap = cv2.VideoCapture(STREAM_URL)
                continue

            count = len(list(out_dir.glob("*.jpg")))
            cv2.putText(frame, f"{sys.argv[1]}: {count}   led {'on' if led else 'off'}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            if time.time() < msg_until:
                cv2.putText(frame, msg, (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            for i, (name, (value, _, _, _step, down, up)) in enumerate(PARAMS.items()):
                cv2.putText(frame, f"[{down}/{up}] {name} {value}", (10, 380 + 25 * i),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            cv2.imshow("shoot", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord(" "):
                # отдельный /capture: JPEG сохраняется байт в байт, без пересжатия.
                # Поток на время снимка закрываем, чтобы не делил с ним Wi-Fi.
                # Вспышку НЕ гасим: на снимке должен быть тот же свет, что на превью.
                cap.release()
                r = requests.get(CAPTURE_URL, timeout=40)
                cap = cv2.VideoCapture(STREAM_URL)
                r.raise_for_status()

                path = out_dir / f"esp_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.jpg"
                path.write_bytes(r.content)
                msg, msg_until = f"saved {path.name}", time.time() + 1.5
            elif key == ord("l"):
                led = 1 - led
                control("led", led)
            elif key == ord("q"):
                break
            else:
                for name, p in PARAMS.items():
                    step = {ord(p[4]): -p[3], ord(p[5]): p[3]}.get(key)
                    if step and p[1] <= p[0] + step <= p[2]:
                        p[0] += step
                        control(name, p[0])
    finally:
        cap.release()
        cv2.destroyAllWindows()
        control("led", 0)


if __name__ == "__main__":
    main()
