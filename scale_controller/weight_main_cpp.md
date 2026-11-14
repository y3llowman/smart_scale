main.cpp для камеры
```cpp
#include <Arduino.h>
#include "HX711.h"

HX711 scale;

void setup() {
  Serial.begin(115200);
  delay(3000);                 // время открыть монитор
  Serial.println("=== Старт ===");
  scale.begin(13, 14);         // DT, SCK
  pinMode(4, OUTPUT);          // вспышка выключена явно, иначе IO4 «висит» и она тускло светится от наводок
  digitalWrite(4, LOW);
}

void loop() {
  if (scale.is_ready()) {
    long raw = scale.read();
    Serial.printf("raw = %ld   (0x%06lX)\n", raw, raw & 0xFFFFFF);
  } else {
    Serial.println("HX711 не отвечает");
  }
  delay(500);
}

```