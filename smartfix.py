#!/usr/bin/python3
'''  cycle through disk errors and repair them '''
import subprocess
import time
import sys

def variables():
    global yn, pendline, sector, pendcount, device
    yn = ""
    pendline = ""
    sector = 0
    pendcount = -1

def get_smart():
    global yn, pendcount, pendline, sector
    process = subprocess.Popen(["smartctl", "-t", "short", device], stdout=subprocess.PIPE)
    process.wait()
    time.sleep(4)
    process = subprocess.Popen(["smartctl", "-a", device], stdout=subprocess.PIPE)
    for line in process.stdout:
        i = line.strip().decode()
        if "Self-test routine in progress" in i:
            if pendcount < 1 or yn == "redo":
                raise Exception(f"{i}\t{pendcount}\t{yn}")
            yn = "redo"
            print(f"{i}\t{pendcount}")
            time.sleep(3.55)
            return
        elif "Current_Pending_Sector" in i:
            a = i.split(" ")
            pendcount = int(a[9])
        elif "# 1" in i:
            if "Completed: read failure" in i:
                pendline = i
                a = i.split(" ")
                sector = int(a[9])
                yn = sector
                return
            else:
                raise Exception(i)
        yn = i
    process.stdout.close()
    process.wait()

def fix_sector():
    output = subprocess.check_output(["hdparm", "--yes-i-know-what-i-am-doing", "--repair-sector", str(sector), device])
    print(f"Hdparm status {output}")

# MAIN
variables()
device = "/dev/sda" if len(sys.argv) < 2 else sys.argv[1]

print(f"Device: {device}")
continue_prompt = input("Continue? (y/n): ")
if continue_prompt.lower() != "y":
    exit()

get_smart()
pendcounter = pendcount
if pendcount == -1:
    raise Exception("pending sector count negative")
while pendcounter > 0:
    print(f"{pendcount}\t{pendline}\t{sector}")
    fix_sector()
    pendcounter -= 1
    get_smart()

exit()
