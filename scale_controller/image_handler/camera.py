"""
Получение кадров с ESP32-CAM в Python.
pip install opencv-python numpy requests
"""
import socket

import cv2
import numpy as np
import keyboard
import requests

# Плата объявляет себя в сети как esp32cam.local (mDNS), IP узнаём один раз при импорте:
# так быстрее, чем резолвить .local на каждый запрос
# VPN (Happ/sing-box) подмешивает фиктивный адрес 127.0.0.200 — берём только настоящий, не 127.*
try:
    addrs = socket.gethostbyname_ex("esp32cam.local")[2]
except socket.gaierror:
    addrs = []
addrs = [a for a in addrs if not a.startswith("127.")]
if not addrs:
    raise SystemExit("Плата esp32cam.local не найдена: она включена и в той же Wi-Fi сети, что и Mac?")
ESP_IP = addrs[0]
CAPTURE_URL = f"http://{ESP_IP}/capture"
STREAM_URL = f"http://{ESP_IP}:81/stream"


def get_frame() -> np.ndarray:
    """Один кадр как numpy-массив BGR (формат OpenCV)."""
    r = requests.get(CAPTURE_URL, timeout=5)
    r.raise_for_status()
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("Не удалось декодировать JPEG")
    return img

def show_img(img):
    print("Кадр:", img.shape)
    cv2.imwrite("snapshot.jpg", img)

def run_stream():
    """Непрерывный поток: читаем кадры и обрабатываем каждый."""
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        raise RuntimeError(f"Не удалось открыть поток {STREAM_URL}")

    while True:
        ok, frame = cap.read()  # frame: np.ndarray (H, W, 3), BGR
        if not ok:
            print("Кадр не получен, переподключение...")
            cap.release()
            cap = cv2.VideoCapture(STREAM_URL)
            continue

        # ---- здесь ваша обработка ----
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 100, 200)
        # -------------------------------

        cv2.imshow("ESP32-CAM", frame)
        cv2.imshow("Edges", edges)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    keyboard.add_hotkey("a", show_img)
    keyboard.add_hotkey("s", run_stream)
    
    print('Hotkeys:\n"a" - take photo\n"s" - run stream\n"q" - quit stream')
