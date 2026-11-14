"""
Помощник для настройки фокуса объектива OV2640 под штрихкоды.
Показывает поток 1280x1024 со вспышкой, резкость центра кадра и пробует прочитать штрихкод.
Крутите объектив и следите, чтобы "резкость" росла. q — выход.
"""
import cv2
import requests
import zxingcpp

from camera import ESP_IP

CONTROL_URL = f"http://{ESP_IP}/control"
STREAM_URL = f"http://{ESP_IP}:81/stream"


def control(var: str, val: int):
    requests.get(CONTROL_URL, params={"var": var, "val": val}, timeout=5).raise_for_status()


def sharpness(gray) -> float:
    """Дисперсия лапласиана в центральной трети кадра: чем больше, тем резче."""
    h, w = gray.shape
    roi = gray[h // 3: 2 * h // 3, w // 3: 2 * w // 3]
    return cv2.Laplacian(roi, cv2.CV_64F).var()


def main():
    control("framesize", 15)  # SXGA 1280x1024
    control("led", 1)
    cap = cv2.VideoCapture(STREAM_URL)
    best = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = cv2.VideoCapture(STREAM_URL)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            s = sharpness(gray)
            best = max(best, s)
            codes = [b.text for b in zxingcpp.read_barcodes(gray)]

            h, w = gray.shape
            cv2.rectangle(frame, (w // 3, h // 3), (2 * w // 3, 2 * h // 3), (0, 255, 255), 2)
            cv2.putText(frame, f"sharpness {s:6.0f}  best {best:6.0f}", (20, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 255), 3)
            cv2.putText(frame, f"barcode: {codes[0] if codes else '-'}", (20, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0) if codes else (0, 0, 255), 3)
            cv2.imshow("focus", cv2.resize(frame, None, fx=0.6, fy=0.6))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
        control("led", 0)
        control("framesize", 10)  # обратно VGA


if __name__ == "__main__":
    main()
