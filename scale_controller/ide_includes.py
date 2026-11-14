# pyright: reportUndefinedVariable=false
# (Import и env подставляет PlatformIO/SCons при сборке — Pylance о них не знает)
# Arduino-ESP32 3.x передаёт пути ESP-IDF через "-iprefix <dir> @flags/includes",
# а IntelliSense (C/C++ в VS Code) такой файл не раскрывает → красные #include.
# Скрипт добавляет эти пути в includePath только при генерации .vscode,
# на сборку прошивки не влияет.
import os

Import("env")

if env.IsIntegrationDump():
    libs_dir = env.PioPlatform().get_package_dir("framework-arduinoespressif32-esp32-libs")
    with open(os.path.join(libs_dir, "flags", "includes")) as f:
        tokens = f.read().split()
    prefix = os.path.join(libs_dir, "include")
    env.Append(CPPPATH=[
        os.path.join(prefix, path)
        for flag, path in zip(tokens, tokens[1:])
        if flag == "-iwithprefixbefore"
    ])
