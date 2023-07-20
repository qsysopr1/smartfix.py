#!/usr/bin/python3
'''  cycle through disk errors and repair them '''
import subprocess
import time
import sys
import json

def variables():
    global yn, pendline, sector, pendcount, device, DEBUG
    yn = ""
    pendline = ""
    sector = 0
    pendcount = -1
    DEBUG = True

def debug_print(message):
    if DEBUG:
        print(message)

def get_smart():
    global yn, pendcount, pendline, sector
    process = subprocess.Popen(["smartctl", "-t", "short", device], stdout=subprocess.PIPE)
    process.wait()
    time.sleep(4)
    
    process = subprocess.Popen(["smartctl", "-A", "-l", "selftest", "-j", device], stdout=subprocess.PIPE)
    output, _ = process.communicate()
    smart_data = json.loads(output)
    
    pending_sector_info = None
    if 'table' in smart_data['ata_smart_attributes']:
        for attribute in smart_data['ata_smart_attributes']['table']:
            if attribute['id'] == 197:  # Current_Pending_Sector
                pending_sector_info = attribute
                break
    
    if pending_sector_info is None:
        raise Exception("Current_Pending_Sector attribute not found")
    
    pendcount = pending_sector_info['raw']['value']
    debug_print(f"Pending sector count: {pendcount}")
    
    if pendcount < 1 or yn == "redo":
        raise Exception("No new pending sectors are detected or selftest in progress")
    
    try:
        self_test_status = smart_data['ata_smart_self_test_log']['standard']['table']
        for log in self_test_status:
            if log['status']['string'] == "Completed: read failure":
                sector = int(log['lba'])
                pendline = log['status']['string']
                yn = sector
                debug_print(f"Pending sector: {sector}")
                return
    except KeyError:
        pass
    
    yn = "No pending sectors or read failures found"
    debug_print(yn)

    # Debug output to see intermediate values

def fix_sector():
    global sector
    debug_print(f"fix_sector {sector}")
    if DEBUG:
        choice = input(f"Fix sector {sector}? (Y/n/x): ")
        if choice.lower() == 'n':
            print("Skipping sector fix.")
            return
        elif choice.lower() == 'x':
            exit
        else:
            output = subprocess.check_output(["hdparm", "--yes-i-know-what-i-am-doing", "--repair-sector", str(sector), device])
            print(output)

# MAIN
variables()
device = "/dev/sda" if len(sys.argv) < 2 else sys.argv[1]

print(f"Device: {device}")
continue_prompt = input("Continue? (y/n): ")
if continue_prompt.lower() != "y":
    exit()

try:
    get_smart()
except Exception as e:
    print(f"Error: {str(e)}")
    exit()

pendcounter = pendcount
print(f"pendcounter {pendcount}")
if pendcount == -1:
    raise Exception(f"Pending sector count is negative: {pendcount}")
while pendcounter > 0:
    print(f"{pendcount}\t{pendline}\t{sector}")
    fix_sector()
    pendcounter -= 1
    if pendcounter == 0:
        break
    get_smart()

exit()
