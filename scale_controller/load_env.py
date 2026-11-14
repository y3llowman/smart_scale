# pyright: reportUndefinedVariable=false
# (Import и env подставляет PlatformIO/SCons при сборке — Pylance о них не знает)
# Wi-Fi из .env (лежит рядом с platformio.ini, в git не попадает) →
# макросы WIFI_SSID / WIFI_PASS для main.cpp.
import os

Import("env")

path = os.path.join(env.subst("$PROJECT_DIR"), ".env")
if not os.path.exists(path):
    raise SystemExit("Нет файла .env: создайте его со строками WIFI_SSID=... и WIFI_PASS=...")

with open(path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env.Append(CPPDEFINES=[(key.strip(), env.StringifyMacro(value.strip()))])
