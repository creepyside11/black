import time
import os

# To cross 1000m fast without getting stuck on foot collision:
# We step by 30-50m intervals in vehicle/onfoot sync
with open("controller.pid") as f:
    pid = int(f.read().strip())

# Check controller process
print(f"Controller PID: {pid}")

# Write command to controller to fast step to Arzamas
with open("manual_cmd.txt", "w", encoding="utf-8") as f:
    f.write("setpos 2280.0 -1450.0 25.0")

time.sleep(1)
with open("manual_cmd.txt", "w", encoding="utf-8") as f:
    f.write("pos")
