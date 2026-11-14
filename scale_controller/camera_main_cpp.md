```cpp
// ESP32-CAM (AI Thinker): раздаёт кадры по Wi-Fi
//   http://<IP>/capture      — один JPEG-кадр
//   http://<IP>:81/stream    — непрерывный MJPEG-поток
// Положить в src/main.cpp


#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
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


static httpd_handle_t capture_server = NULL;
static httpd_handle_t stream_server  = NULL;


// ---------- /capture : один кадр в максимальном разрешении ----------
// Стрим идёт в 640x480 ради скорости; на время снимка камера переключается в UXGA и обратно
#define STREAM_QUALITY  20   // сильнее сжатие → кадр стрима ~8 КБ, быстро даже на плохом Wi-Fi
#define CAPTURE_QUALITY 10
static esp_err_t capture_handler(httpd_req_t* req) {
sensor_t* s = esp_camera_sensor_get();
framesize_t stream_size = s->status.framesize;
int stream_quality = s->status.quality;
s->set_quality(s, CAPTURE_QUALITY);
s->set_framesize(s, FRAMESIZE_UXGA);

  // Первые кадры после переключения — старого размера или с неустоявшейся экспозицией:
  // берём третий кадр нужного размера
camera_fb_t* fb = NULL;
int good = 0;
for (int i = 0; i < 15 && good < 3; i++) {
if (fb) esp_camera_fb_return(fb);
fb = esp_camera_fb_get();
if (fb && fb->width == 1600) good++;
  }

  // Копируем кадр и сразу возвращаем стрим в быстрый режим — отправка по Wi-Fi может идти секунды
uint8_t* jpg = NULL;
size_t len = 0;
if (good == 3 && (jpg = (uint8_t*)ps_malloc(fb->len))) {
memcpy(jpg, fb->buf, fb->len);
len = fb->len;
  }
if (fb) esp_camera_fb_return(fb);
s->set_framesize(s, stream_size);
s->set_quality(s, stream_quality);

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


// ---------- /control?var=framesize|quality&val=N : настройки на лету ----------
// framesize: 10 VGA 640x480, 11 SVGA 800x600, 12 XGA 1024x768, 14 SXGA 1280x1024, 15 UXGA 1600x1200
// quality: 4..63, меньше = лучше качество и больше кадр
// led: 0/1 — светодиод-вспышка на GPIO 4
// Яркость (по умолчанию всё автоматически):
//   ae_level -2..2 — поправка автоэкспозиции (+ светлее), brightness -2..2 — яркость после обработки,
//   aec2 0/1 — «ночной» режим автоэкспозиции (длиннее выдержка в темноте),
//   gainceiling 0..6 — предел усиления 2x..128x (больше — светлее, но шумнее)
#define LED_GPIO_NUM 4
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
else if (!strcmp(var, "brightness")) res = s->set_brightness(s, v);
else if (!strcmp(var, "aec2")) res = s->set_aec2(s, v);
else if (!strcmp(var, "gainceiling") && v >= 0 && v <= 6) res = s->set_gainceiling(s, (gainceiling_t)v);
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
httpd_uri_t stream_uri = { "/stream", HTTP_GET, 
stream_handler, NULL };
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
c.jpeg_quality = 0;                // 0..63, меньше = лучше качество
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
  }
return true;
}


void setup() {
Serial.begin(115200);
pinMode(LED_GPIO_NUM, OUTPUT);
digitalWrite(LED_GPIO_NUM, LOW);

// подключение весов
delay(3000);                 // время открыть монитор
Serial.println("=== Старт ===");
scale.begin(13, 14);         // DT, SCK
pinMode(4, OUTPUT);          // вспышка выключена явно, иначе IO4 «висит» и она тускло светится от наводок
digitalWrite(4, LOW);

if (!init_camera()) {
delay(3000);
ESP.restart();
  }


WiFi.mode(WIFI_STA);
WiFi.setSleep(false);  // меньше задержек в стриме
WiFi.begin(WIFI_SSID_LUB, WIFI_PASS_LUB);
Serial.print("Connecting to Wi-Fi");
while (WiFi.status() != WL_CONNECTED) {
delay(500);
Serial.print(".");
  }
Serial.println();


start_servers();

  // Имя в локальной сети: http://esp32cam.local — IP можно не знать
if (MDNS.begin("esp32cam")) {
MDNS.addService("http", "tcp", 80);
  } else {
Serial.println("mDNS failed");
  }


String ip = WiFi.localIP().toString();
  // Сила сигнала: > -60 хорошо, -60..-75 терпимо, < -75 плохо (антенна/питание)
Serial.printf("Wi-Fi RSSI: %d dBm\n", WiFi.RSSI());
Serial.println("Camera ready:");
Serial.println("  http://" + ip + "/capture");
Serial.println("  http://" + ip + ":81/stream");
Serial.println("  http://esp32cam.local/capture");
}


void loop() {
if (scale.is_ready()) {
    long raw = scale.read();
    // Serial.printf("raw = %ld   (0x%06lX)\n", raw, raw & 0xFFFFFF);
    Serial.cout("\rraw = %ld   (0x%06lX)\n", raw, raw & 0xFFFFFF);
} else {
    Serial.println("HX711 не отвечает");
}
    delay(500);
}
```