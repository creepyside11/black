import time
import os
import sys

# Банк Арзамаса: вход / крыльцо банка в центре Арзамаса
# Координаты: X = 2280.0, Y = -1450.0, Z = 25.0
target_x, target_y, target_z = 2280.0, -1450.0, 25.0

print(f"[*] Отправляем команду движения в Банк Арзамаса: {target_x}, {target_y}, {target_z}")
with open("manual_cmd.txt", "w", encoding="utf-8") as f:
    f.write(f"goto {target_x} {target_y} {target_z}")

time.sleep(2)
with open("manual_cmd.txt", "w", encoding="utf-8") as f:
    f.write("pos")

time.sleep(2)
