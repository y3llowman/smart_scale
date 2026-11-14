// ESP32-CAM (AI Thinker) — кухонные весы с камерой, дисплеем и сканером ШК
//
//   http://<IP>/capture      — один JPEG-кадр (UXGA)
//   http://<IP>:81/stream    — непрерывный MJPEG-поток (VGA)
//   http://<IP>/control?...  — настройки камеры на лету
//
// Периферия по текущей схеме:
//   HX711        DT = GPIO13, SCK = GPIO14   (тензодатчик 5 кг)
//   OLED 128x64  SDA = GPIO12, SCL = GPIO15  (7-пиновый модуль в режиме I2C:
//                RES подтянут к VDD, DC и CS — к GND)
//   Сканер ШК    TX сканера = GPIO16 (RX второго UART), RX сканера = 3V3
//                (сканер только передаёт; UART0/GPIO1/3 остаётся USB-консолью)
//
// ВАЖНО про GPIO12: на этой конкретной плате вход GPIO12 повреждён (см. CLAUDE.md).
// Если дисплей не определяется — переставь провод SDA с GPIO12 на GPIO2: прошивка
// при старте сама перебирает пары 12/15, 15/12, 2/15, 15/2 и найдёт его.
//
// Консоль 115200. Команды: h — помощь, t — тара, c<граммы> — калибровка,
// w — показать вес сейчас, i — скан I2C, p<gpio> — перевесить RX сканера,
// b<baud> — скорость сканера, e/d — включить/выключить сканер, o<sda> <scl> — переинициализировать OLED.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "HX711.h"
#include "esp_camera.h"
#include "esp_http_server.h"


// ==== Wi-Fi (только 2.4 ГГц) — задаётся в .env, макросы подставляет load_env.py ====
#if !defined(WIFI_SSID_LUB) || !defined(WIFI_PASS_LUB)
#error "Задайте WIFI_SSID и WIFI_PASS в файле .env"
#endif


// ==== Пины камеры AI Thinker ESP32-CAM ====
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

#define LED_GPIO_NUM       4   // вспышка


// ==== Пины периферии (текущая схема) ====
#define HX711_DT_PIN      13
#define HX711_SCK_PIN     14

#define OLED_SDA_PIN      12   // по схеме; см. предупреждение про IO12 выше
#define OLED_SCL_PIN      15
#define OLED_WIDTH       128
#define OLED_HEIGHT       64

#define SCANNER_RX_PIN    16   // TX сканера → GPIO16
#define SCANNER_TX_PIN    -1   // RX сканера не управляем (подтянут к 3V3)
#define SCANNER_BAUD      9600 // GM65/GM803 по умолчанию 9600 8N1


// ==== Настройки весов ====
#define WEIGHT_READ_MS     500
#define DISPLAY_MS         250
// Пока не откалибровано, на дисплее показываем raw, а не граммы. После команды c<граммы>
// коэффициент сохраняется в NVS и переживает перезагрузку.
#define DEFAULT_FACTOR   -7050.0f


static httpd_handle_t capture_server = NULL;
static httpd_handle_t stream_server  = NULL;

HX711 scale;
Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
HardwareSerial scannerSerial(2);     // вход сканера: GPIO16
Preferences prefs;

// --- состояние весов ---
static bool  scaleOk        = false;
static bool  calibrated     = false;
static long  tareOffset     = 0;
static float calFactor      = DEFAULT_FACTOR;
static long  lastRaw        = 0;
static float weightG        = 0.0f;
static float weightFiltered = 0.0f;
static unsigned long lastWeightRead = 0;
static unsigned long lastHx711Error = 0;

// --- состояние дисплея ---
static bool oledOk = false;
static bool cameraOk = false;

// Диагностика порчи кучи: печатает free heap и проверяет целостность.
static void heapCheck(const char* tag) {
  Serial.printf("[HEAP] %s: free=%u", tag, ESP.getFreeHeap());
  if (heap_caps_check_integrity_all(true)) {
    Serial.println(" intact");
  } else {
    Serial.println(" CORRUPTED!");
  }
}

// --- состояние сканера ---
static bool scannerEnabled = true;
static int  scannerRxPin   = SCANNER_RX_PIN;
static long scannerBaud    = SCANNER_BAUD;
static String scanLine;
static unsigned long lastScanByte = 0;  // для сканеров без \r\n в конце кода
static String lastBarcode;
static unsigned long lastBarcodeAt = 0;
static unsigned long barcodeCount  = 0;

// --- консоль ---
static String consoleLine;
static unsigned long lastDisplay = 0;


// =====================================================================
//  OLED
// =====================================================================

static void drawDisplay() {
  if (!oledOk) return;

  char buf[32];
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);

  oled.setCursor(0, 0);
  oled.print(F("SMART SCALE"));
  oled.drawLine(0, 9, OLED_WIDTH - 1, 9, SSD1306_WHITE);

  oled.setCursor(0, 12);
  if (WiFi.status() == WL_CONNECTED) {
    oled.print(WiFi.localIP());
  } else {
    oled.print(F("WiFi: connecting..."));
  }

  // Вес крупно
  oled.setCursor(0, 24);
  oled.setTextSize(2);
  if (calibrated) {
    snprintf(buf, sizeof(buf), "%7.1f g", weightFiltered);
  } else {
    snprintf(buf, sizeof(buf), "%7ld", lastRaw);
  }
  oled.print(buf);

  oled.setTextSize(1);
  oled.setCursor(0, 44);
  if (calibrated) {
    snprintf(buf, sizeof(buf), "raw %ld", lastRaw);
  } else {
    snprintf(buf, sizeof(buf), "raw, no cal");
  }
  oled.print(buf);

  oled.setCursor(0, 54);
  if (lastBarcode.length()) {
    snprintf(buf, sizeof(buf), "%.20s", lastBarcode.c_str());
  } else {
    snprintf(buf, sizeof(buf), "code: -");
  }
  oled.print(buf);

  oled.display();
}

// Дисплей на шине I2C0 (Wire). Камера занимает I2C1 для SCCB (CONFIG_SCCB_HARDWARE_I2C_PORT1),
// поэтому I2C1 не трогаем — иначе получаем "I2C bus id(1) already acquired" и порчу кучи.
// Только ОДИН Wire.begin() без Wire.end()/повторного begin: повторный init/deinit I2C
// в Arduino 3.x портит кучу (то самое LoadProhibited дальше в Wi-Fi).
static bool displayInit() {
  if (!Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN, 400000)) {
    Serial.println("[OLED] I2C0 не поднялась");
    return false;
  }

  for (uint8_t addr : {0x3C, 0x3D}) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() != 0) continue;

    // periphBegin = false: Wire уже настроен на нужные пины, begin() его не сбросит
    if (!oled.begin(SSD1306_SWITCHCAPVCC, addr, true, false)) continue;

    oledOk = true;
    Serial.printf("[OLED] найден: SDA=%d SCL=%d addr=0x%02X\n", OLED_SDA_PIN, OLED_SCL_PIN, addr);
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);
    oled.setCursor(0, 0);
    oled.println(F("SMART SCALE"));
    oled.println(F("boot..."));
    oled.display();
    return true;
  }

  oledOk = false;
  Serial.println("[OLED] не найден. Проверь питание 3V3 и адрес 0x3C/0x3D.");
  Serial.println("[OLED] Если SDA на GPIO12 — переставь на GPIO2 и выполни `o2 15`.");
  return false;
}

static void i2cScan() {
  Serial.println("[I2C] сканирую шину...");
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C] устройство по адресу 0x%02X\n", addr);
      found++;
    }
  }
  if (!found) Serial.println("[I2C] ничего не найдено");
}


// =====================================================================
//  HX711 / вес
// =====================================================================

static void scaleInit() {
  prefs.begin("scale", false);
  tareOffset = prefs.getLong("offset", 0);
  calFactor  = prefs.getFloat("factor", DEFAULT_FACTOR);
  calibrated = prefs.getBool("cal", false);

  scale.begin(HX711_DT_PIN, HX711_SCK_PIN);
  scale.set_scale(calFactor);

  // HX711 выдаёт первое преобразование примерно через 100 мс после включения
  scaleOk = scale.wait_ready_timeout(1000);
  if (scaleOk) {
    Serial.printf("[HX711] ok: DT=%d SCK=%d, offset=%ld, factor=%.2f, cal=%s\n",
                  HX711_DT_PIN, HX711_SCK_PIN, tareOffset, calFactor,
                  calibrated ? "yes" : "no");
  } else {
    Serial.println("[HX711] НЕ отвечает. Проверь DT=GPIO13, SCK=GPIO14, питание 3V3 и пайку датчика.");
  }
}

static void readWeight() {
  if (!scaleOk) {
    // Иногда HX711 «просыпается» позже — периодически проверяем снова
    if (scale.wait_ready_timeout(0)) {
      scaleOk = true;
      Serial.println("[HX711] связь восстановлена");
    } else {
      if (millis() - lastHx711Error > 2000) {
        lastHx711Error = millis();
        Serial.println("[HX711] нет данных: проверь DT/SCK и общий GND");
      }
      return;
    }
  }

  if (!scale.is_ready()) return;

  long raw = scale.read();
  if (raw == 0) return;  // явный сбой чтения

  lastRaw = raw;
  weightG = (raw - tareOffset) / calFactor;
  if (isnan(weightG) || isinf(weightG)) weightG = 0;

  // Немного сглаживаем, чтобы цифры на дисплее не прыгали
  if (weightFiltered == 0.0f) weightFiltered = weightG;
  else weightFiltered = weightFiltered * 0.7f + weightG * 0.3f;

  Serial.printf("[HX711] raw=%ld  weight=%.1f g  (offset=%ld, factor=%.1f%s)\n",
                lastRaw, weightFiltered, tareOffset, calFactor,
                calibrated ? "" : ", НЕ КАЛИБРОВАНО");
}

static void doTare() {
  if (!scaleOk) {
    Serial.println("[HX711] тара невозможна: нет связи");
    return;
  }
  Serial.println("[HX711] тарирую, платформу не трогать...");
  long avg = scale.read_average(20);
  if (avg == 0) {
    Serial.println("[HX711] тара не удалась (нули)");
    return;
  }
  tareOffset = avg;
  prefs.putLong("offset", tareOffset);
  prefs.putFloat("factor", calFactor);
  prefs.putBool("cal", calibrated);
  Serial.printf("[HX711] тара сохранена: offset=%ld\n", tareOffset);
}

static void doCalibrate(float grams) {
  if (!scaleOk) {
    Serial.println("[HX711] калибровка невозможна: нет связи");
    return;
  }
  if (grams <= 0 || grams > 20000) {
    Serial.println("[HX711] укажи массу в граммах: c500");
    return;
  }
  Serial.printf("[HX711] калибровка по %.1f г, не трогать...\n", grams);
  long avg = scale.read_average(20);
  if (avg == 0) {
    Serial.println("[HX711] калибровка не удалась (нули)");
    return;
  }
  float delta = (float)(avg - tareOffset);
  if (fabs(delta) < 100.0f) {
    Serial.println("[HX711] сигнал слишком мал: проверь, что груз стоит на платформе");
    return;
  }
  calFactor = delta / grams;
  calibrated = true;
  prefs.putFloat("factor", calFactor);
  prefs.putBool("cal", true);
  Serial.printf("[HX711] готово: factor=%.3f (raw %ld, offset %ld)\n",
                calFactor, avg, tareOffset);
}


// =====================================================================
//  Сканер штрихкодов (UART2)
// =====================================================================

static void scannerStart() {
  // UART2: основной вариант — TX сканера на GPIO16
  scannerSerial.end();
  scannerSerial.setRxBufferSize(1024);  // чтобы не терять код, пока камера отдаёт кадр
  scannerSerial.begin(scannerBaud, SERIAL_8N1, scannerRxPin, SCANNER_TX_PIN);

  Serial.printf("[SCAN] UART2: RX=GPIO%d, %ld 8N1\n", scannerRxPin, scannerBaud);
  Serial.println("[SCAN] если кода нет — проверь TX сканера и скорость (b9600/b115200)");
}

static void handleBarcode(String code) {
  code.trim();
  if (code.length() < 3) return;

  // Защита от повторов: один и тот же код чаще раза в 0.8 с не печатаем
  if (code == lastBarcode && millis() - lastBarcodeAt < 800) return;

  lastBarcode = code;
  lastBarcodeAt = millis();
  barcodeCount++;
  Serial.printf("[BARCODE] %s\n", code.c_str());
}

static void pollScannerPort(HardwareSerial &port, String &line) {
  while (port.available()) {
    int c = port.read();
    if (c < 0) break;
    lastScanByte = millis();
    if (c == '\r' || c == '\n') {
      handleBarcode(line);
      line = "";
    } else if (isPrintable(c)) {
      line += (char)c;
      if (line.length() > 96) line.remove(0, line.length() - 96);
    }
  }
}

static void pollScanner() {
  if (!scannerEnabled) return;
  pollScannerPort(scannerSerial, scanLine);

  // Некоторые сканеры не добавляют \r\n: если байты замолчали на 150 мс — считаем строку готовой
  if (scanLine.length() && millis() - lastScanByte > 150) {
    handleBarcode(scanLine);
    scanLine = "";
  }
}


// =====================================================================
//  Консольные команды
// =====================================================================

static void printHelp() {
  Serial.println();
  Serial.println("=== Команды ===");
  Serial.println("  w           показать raw и вес сейчас");
  Serial.println("  t           тара (пустая платформа)");
  Serial.println("  c<граммы>   калибровка по эталону, например c500");
  Serial.println("  f<число>    задать коэффициент вручную, например f-7050");
  Serial.println("  i           просканировать I2C");
  Serial.println("  p<gpio>     перевесить RX сканера (p3 = U0R; ввод команд в консоль отключится до сброса)");
  Serial.println("  b<baud>     скорость сканера, например b9600");
  Serial.println("  e / d       включить / выключить сканер");
  Serial.println("  o<sda> <scl> переинициализировать OLED, например o2 15");
  Serial.println("  h           эта справка");
  Serial.println();
}

static void handleCommand(String cmd) {
  cmd.trim();
  if (!cmd.length()) return;

  char c = tolower(cmd[0]);

  switch (c) {
    case 'h':
      printHelp();
      break;

    case 'w':
      if (scaleOk && scale.is_ready()) {
        long raw = scale.read();
        lastRaw = raw;
        weightG = (raw - tareOffset) / calFactor;
        weightFiltered = weightG;
        Serial.printf("[HX711] raw=%ld  weight=%.1f g\n", raw, weightG);
      } else {
        Serial.println("[HX711] нет данных");
      }
      break;

    case 't':
      doTare();
      break;

    case 'i':
      i2cScan();
      break;

    case 'e':
      scannerEnabled = true;
      scannerStart();
      Serial.println("[SCAN] включён");
      break;

    case 'd':
      scannerEnabled = false;
      Serial.println("[SCAN] выключен");
      break;

    case 'c': {
      float grams = cmd.substring(1).toFloat();
      doCalibrate(grams);
      break;
    }

    case 'f': {
      float f = cmd.substring(1).toFloat();
      if (f == 0) {
        Serial.println("[HX711] коэффициент не может быть 0");
      } else {
        calFactor = f;
        calibrated = true;
        prefs.putFloat("factor", calFactor);
        prefs.putBool("cal", true);
        Serial.printf("[HX711] factor=%.3f\n", calFactor);
      }
      break;
    }

    case 'p': {
      int pin = cmd.substring(1).toInt();
      if (pin >= 0 && pin <= 39) {
        scannerRxPin = pin;
        scannerStart();
      } else {
        Serial.println("[SCAN] неверный номер GPIO");
      }
      break;
    }

    case 'b': {
      long baud = cmd.substring(1).toInt();
      if (baud >= 1200 && baud <= 115200) {
        scannerBaud = baud;
        scannerStart();
      } else {
        Serial.println("[SCAN] скорость 1200..115200");
      }
      break;
    }

    case 'o': {
      int sda = -1, scl = -1;
      int sp = cmd.indexOf(' ');
      if (sp > 0) {
        sda = cmd.substring(1, sp).toInt();
        scl = cmd.substring(sp + 1).toInt();
      }
      if (sda >= 0 && scl >= 0 && sda != scl) {
        Wire.end();
        if (Wire.begin(sda, scl, 400000)) {
          oledOk = false;
          for (uint8_t addr : {0x3C, 0x3D}) {
            Wire.beginTransmission(addr);
            if (Wire.endTransmission() == 0 &&
                oled.begin(SSD1306_SWITCHCAPVCC, addr, true, false)) {
              oledOk = true;
              Serial.printf("[OLED] ok: SDA=%d SCL=%d addr=0x%02X\n", sda, scl, addr);
              break;
            }
          }
        }
        if (!oledOk) Serial.println("[OLED] не найден на этих пинах");
      } else {
        Serial.println("[OLED] формат: o2 15");
      }
      break;
    }

    default: {
      // Если по UART0 прилетел штрихкод (сканер на GPIO3), не ругаемся «неизвестная команда»
      bool onlyDigits = cmd.length() >= 6;
      for (size_t i = 0; i < cmd.length(); i++) {
        if (!isdigit((unsigned char)cmd[i])) { onlyDigits = false; break; }
      }
      if (!onlyDigits) Serial.println("Неизвестная команда. h — помощь.");
      break;
    }
  }
}

static void pollConsole() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      handleCommand(consoleLine);
      consoleLine = "";
    } else if (isPrintable(c) && consoleLine.length() < 64) {
      consoleLine += c;
    }
  }
}


// =====================================================================
//  Камера
// =====================================================================

// Фиксированные настройки света и цвета для съёмки еды. У весов свет свой (вспышка GPIO 4),
// поэтому авто-WB и автоэкспозиция дают «плывущий» цвет и тёмные кадры (~30/255).
// set_framesize() сбрасывает регистры сенсора на «авто», поэтому helper вызываем
// после каждого переключения размера.
static void apply_camera_defaults(sensor_t* s) {
  s->set_awb_gain(s, 0);       // авто-баланс белого выкл
  s->set_wb_mode(s, 4);        // 4 = «дом»; подобрать 3/4 по белому листу (см. README, «Калибровка»)
  s->set_exposure_ctrl(s, 0);  // автоэкспозиция выкл
  s->set_aec_value(s, 300);    // ручная экспозиция 0..1200 строк; калибровать по гистограмме
  s->set_aec2(s, 0);           // не «ночной» режим — длинная выдержка даёт смаз
  s->set_gain_ctrl(s, 0);      // автоусиление выкл
  s->set_agc_gain(s, 0);       // ручное усиление 0..30; 0 = минимум шума
  s->set_gainceiling(s, GAINCEILING_2X);  // не давать камере «дотягивать» темноту шумом
}

// ---------- /capture : один кадр в максимальном разрешении ----------
// Стрим идёт в 640x480 ради скорости; на время снимка камера переключается в UXGA и обратно
#define STREAM_QUALITY  20   // сильнее сжатие → кадр стрима ~8 КБ, быстро даже на плохом Wi-Fi
#define CAPTURE_QUALITY 10
static esp_err_t capture_handler(httpd_req_t* req) {
  sensor_t* s = esp_camera_sensor_get();
  framesize_t stream_size = s->status.framesize;
  int stream_quality = s->status.quality;
  s->set_framesize(s, FRAMESIZE_UXGA);
  s->set_quality(s, CAPTURE_QUALITY);
  apply_camera_defaults(s);  // set_framesize сбросил регистры на «авто» — фиксируем свет/цвет заново

  // Первые кадры после переключения — старого размера: выкидываем их и берём кадр нужного размера
  camera_fb_t* fb = NULL;
  for (int i = 0; i < 4; i++) {
    fb = esp_camera_fb_get();
    if (fb) {
      esp_camera_fb_return(fb); // Сразу возвращаем, освобождая буфер сенсора
    }
    delay(100); // Вот этот delay критически важен для адаптации матрицы!
  }

  // Теперь берем финальный, стабилизировавшийся кадр
  fb = esp_camera_fb_get();
  if (!fb) {
    // Если не смогли получить кадр, возвращаем настройки стрима и выходим
    s->set_framesize(s, stream_size);
    s->set_quality(s, stream_quality);
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  // Копируем кадр и сразу возвращаем стрим в быстрый режим — отправка по Wi-Fi может идти секунды
  uint8_t* jpg = NULL;
  size_t len = 0;
  if ((jpg = (uint8_t*)ps_malloc(fb->len))) {
    memcpy(jpg, fb->buf, fb->len);
    len = fb->len;
  }

  if (fb) esp_camera_fb_return(fb);
  s->set_framesize(s, stream_size);
  s->set_quality(s, stream_quality);
  apply_camera_defaults(s);  // вернуть стриму тот же фиксированный свет/цвет

  if (!jpg) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char*)jpg, len);
  free(jpg);
  return res;
}


// ---------- /stream : MJPEG ----------
#define BOUNDARY "frame"
static esp_err_t stream_handler(httpd_req_t* req) {
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=" BOUNDARY);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char part[128];

  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) return ESP_FAIL;

    size_t hlen = snprintf(part, sizeof(part),
      "\r\n--" BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
      (unsigned)fb->len);

    esp_err_t res = httpd_resp_send_chunk(req, part, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);
    esp_camera_fb_return(fb);

    if (res != ESP_OK) break;  // клиент отключился
  }
  return ESP_OK;
}


// ---------- /control?var=...&val=N : настройки на лету ----------
// framesize: 10 VGA 640x480, 11 SVGA 800x600, 12 XGA 1024x768, 14 SXGA 1280x1024, 15 UXGA 1600x1200
// quality: 4..63, меньше = лучше качество и больше кадр
// led: 0/1 — светодиод-вспышка на GPIO 4
// Экспозиция (по умолчанию фиксированная — apply_camera_defaults):
//   exposure_ctrl 0/1 — автоэкспозиция выкл/вкл (0 = ручная aec_value)
//   aec_value 0..1200 — ручная экспозиция в строках (больше = светлее)
//   gain_ctrl 0/1 — автоусиление выкл/вкл (0 = ручное agc_gain)
//   agc_gain 0..30 — ручное усиление (больше = светлее, но шумнее)
//   gainceiling 0..6 — предел усиления 2x..128x
//   ae_level -2..2 — поправка автоэкспозиции (+ светлее), brightness -2..2 — яркость после обработки
//   aec2 0/1 — «ночной» режим автоэкспозиции (длинная выдержка в темноте)
// Цвет:
//   wb_mode 0..4 — 0 авто, 1 солнце, 2 пасмурно, 3 офис, 4 дом
//   awb_gain 0/1 — авто-баланс белого выкл/вкл (это флаг, НЕ режим)
static esp_err_t control_handler(httpd_req_t* req) {
  char query[64], var[16], val[8];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "var", var, sizeof(var)) != ESP_OK ||
      httpd_query_key_value(query, "val", val, sizeof(val)) != ESP_OK) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "need ?var=...&val=...");
    return ESP_FAIL;
  }
  sensor_t* s = esp_camera_sensor_get();
  int v = atoi(val);
  int res = -1;
  if (!strcmp(var, "framesize") && v <= FRAMESIZE_UXGA) res = s->set_framesize(s, (framesize_t)v);
  else if (!strcmp(var, "quality") && v >= 4) res = s->set_quality(s, v);
  else if (!strcmp(var, "led")) { digitalWrite(LED_GPIO_NUM, v ? HIGH : LOW); res = 0; }
  else if (!strcmp(var, "ae_level")) res = s->set_ae_level(s, v);
  else if (!strcmp(var, "awb_gain")) res = s->set_awb_gain(s, v);
  else if (!strcmp(var, "brightness")) res = s->set_brightness(s, v);
  else if (!strcmp(var, "aec2")) res = s->set_aec2(s, v);
  else if (!strcmp(var, "gainceiling") && v >= 0 && v <= 6) res = s->set_gainceiling(s, (gainceiling_t)v);
  else if (!strcmp(var, "wb_mode") && v >= 0 && v <= 4) res = s->set_wb_mode(s, v);
  else if (!strcmp(var, "exposure_ctrl")) res = s->set_exposure_ctrl(s, v);
  else if (!strcmp(var, "aec_value") && v >= 0 && v <= 1200) res = s->set_aec_value(s, v);
  else if (!strcmp(var, "agc_gain") && v >= 0 && v <= 30) res = s->set_agc_gain(s, v);
  else if (!strcmp(var, "gain_ctrl")) res = s->set_gain_ctrl(s, v);
  if (res != 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad var or val");
    return ESP_FAIL;
  }
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_sendstr(req, "OK");
}

static void start_servers() {
  // Порт 80: одиночные кадры и настройки
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.send_wait_timeout = 30;  // по умолчанию 5 с: на медленном Wi-Fi кадр 1600x1200 обрывался
  httpd_uri_t capture_uri = { "/capture", HTTP_GET, capture_handler, NULL };
  httpd_uri_t control_uri = { "/control", HTTP_GET, control_handler, NULL };
  if (httpd_start(&capture_server, &cfg) == ESP_OK) {
    httpd_register_uri_handler(capture_server, &capture_uri);
    httpd_register_uri_handler(capture_server, &control_uri);
  }

  // Порт 81: поток (отдельный сервер, чтобы /capture работал во время стрима)
  cfg.server_port = 81;
  cfg.ctrl_port += 1;
  httpd_uri_t stream_uri = { "/stream", HTTP_GET, stream_handler, NULL };
  if (httpd_start(&stream_server, &cfg) == ESP_OK) {
    httpd_register_uri_handler(stream_server, &stream_uri);
  }
}

static bool init_camera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk  = XCLK_GPIO_NUM;
  c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn  = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 10000000;
  c.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    c.frame_size   = FRAMESIZE_UXGA;    // буферы под максимум, чтобы /control мог переключать размер
    c.jpeg_quality = 0;                 // 0..63, меньше = лучше качество
    c.fb_count     = 2;
    c.fb_location  = CAMERA_FB_IN_PSRAM;
    c.grab_mode    = CAMERA_GRAB_LATEST; // всегда свежий кадр
  } else {
    c.frame_size   = FRAMESIZE_QVGA;
    c.jpeg_quality = 15;
    c.fb_count     = 1;
    c.fb_location  = CAMERA_FB_IN_DRAM;
    c.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  if (psramFound()) {
    sensor_t* s = esp_camera_sensor_get();
    s->set_framesize(s, FRAMESIZE_VGA);  // стрим 640x480, /capture сам переключается в UXGA
    s->set_quality(s, STREAM_QUALITY);
    apply_camera_defaults(s);  // фиксированный свет/цвет и для стрима
  }
  return true;
}


// =====================================================================
//  setup / loop
// =====================================================================

void setup() {
  Serial.begin(115200);
  pinMode(LED_GPIO_NUM, OUTPUT);
  digitalWrite(LED_GPIO_NUM, LOW);   // вспышка выключена явно, иначе IO4 «висит» и тускло светится

  delay(3000);                       // время открыть монитор
  Serial.println();
  Serial.println("=== Старт: весы + OLED + сканер ===");

  // 1. Камера — первой: её DMA-буферы должны выделиться на чистой куче.
  heapCheck("до камеры");
  Serial.println("[CAM] init...");
  cameraOk = init_camera();
  if (cameraOk) {
    heapCheck("после камеры");
    Serial.println("[CAM] ok");
  } else {
    Serial.println("[CAM] не поднялась — работаю без камеры (проверь шлейф)");
  }

  // 2. Дисплей (шина I2C0; камера занимает I2C1)
  displayInit();
  heapCheck("после дисплея");

  // 3. Весы
  scaleInit();
  heapCheck("после весов");

  // 4. Сканер
  scannerStart();
  heapCheck("после сканера");
  Serial.println("[SCAN] активен по умолчанию, жду штрихкод...");

  // 5. Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // меньше задержек в стриме
  WiFi.begin(WIFI_SSID_LUB, WIFI_PASS_LUB);
  Serial.print("Connecting to Wi-Fi");
  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 30000) {
    delay(500);
    Serial.print(".");
    drawDisplay();
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("Wi-Fi OK, RSSI: %d dBm\n", WiFi.RSSI());
  } else {
    Serial.println("Wi-Fi не подключился за 30 с — работаю без сети (камера будет недоступна)");
  }

  if (cameraOk) start_servers();

  // Имя в локальной сети: http://esp32cam.local — IP можно не знать
  if (WiFi.status() == WL_CONNECTED) {
    if (MDNS.begin("esp32cam")) {
      MDNS.addService("http", "tcp", 80);
    } else {
      Serial.println("mDNS failed");
    }
    String ip = WiFi.localIP().toString();
    Serial.println("Camera ready:");
    Serial.println("  http://" + ip + "/capture");
    Serial.println("  http://" + ip + ":81/stream");
    Serial.println("  http://esp32cam.local/capture");
  }

  printHelp();
  drawDisplay();
}

void loop() {
  pollConsole();
  pollScanner();

  unsigned long now = millis();

  if (now - lastWeightRead >= WEIGHT_READ_MS) {
    lastWeightRead = now;
    readWeight();
  }

  if (now - lastDisplay >= DISPLAY_MS) {
    lastDisplay = now;
    drawDisplay();
  }

  delay(2);
}
