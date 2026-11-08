import time
import os
import sys

# Track movement of controller_bot to Arzamas Bank (2280.0, -1450.0, 25.0)
target_x, target_y, target_z = 2280.0, -1450.0, 25.0

print(f"Target: ({target_x}, {target_y}, {target_z})")
for _ in range(12):
    with open("manual_cmd.txt", "w", encoding="utf-8") as f:
        f.write("pos")
    time.sleep(2)
    # Read last pos from controller.log
    with open("controller.log", "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    for l in reversed(lines):
        if "Текущие координаты:" in l:
            print(l.strip())
            # Parse coords
            try:
                coords_str = l.split("Текущие координаты:")[1].strip().strip("()")
                parts = [float(x.strip()) for x in coords_str.split(",")]
                dist = ((parts[0] - target_x)**2 + (parts[1] - target_y)**2)**0.5
                print(f"Distance to Bank of Arzamas: {dist:.1f}m")
            except Exception as e:
                pass
            break
    time.sleep(3)
