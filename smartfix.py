#!/usr/bin/python3
'''  cycle through disk errors and repair them '''
import subprocess
import time
import sys
import json
import warnings

def variables():
    global yn, pendline, sector, lastsector, pendcount, device, DEBUG
    yn = ""
    pendline = ""
    sector = 0  
    lastsector = -1
    pendcount = -1
    DEBUG = False

def debug_print(message):
    if DEBUG:
        print(message)

def get_smart():
    global yn, pendcount, pendline, sector
    process = subprocess.Popen(["smartctl", "-t", "short", device], stdout=subprocess.PIPE)
    process.wait()
    while True:
        time.sleep(4)
        process = subprocess.Popen(["smartctl", "-A", "-l", "selftest", "-j", device], stdout=subprocess.PIPE)
        output, _ = process.communicate()
        smart_data = json.loads(output)
        # Check if a self-test is in progress
        try:
            status_message = smart_data['ata_smart_self_test_log']['standard']['table'][0]['status']['string']
            if "Self-test routine in progress" in status_message:
                print("Self-test is still running, waiting...")
                continue  # Continue the loop until the test completes
            else:
                print(f"Self-test status: {status_message}")
                # Check if the self-test completed without error
                if "Completed without error" in status_message:
                    print("Self-test completed without error. No sectors to fix.")
                    sector = 0  # Reset sector if no error
                    pendcount = 0  # Reset pending sector count
                    return  # Exit as there are no issues to fix
                break  # Exit the loop once the self-test is complete
        except KeyError:
            print("Error: Self-test status not found.")
            break  # Exit the loop if no status is found, or handle the error accordingly
    
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

def fix_sector():
    global sector, lastsector
    if sector == 0:
        raise Exception("Zero sector number")
    
    # Check if the same sector was corrected in the previous iteration
    if sector == lastsector:
        print(f"Skipping sector {sector}: already corrected in the previous iteration.")
        return
    
    debug_print(f"fix_sector {sector}")
    
    if DEBUG:
        choice = input(f"Fix sector {sector}? (Y/n/x): ")
        if choice.lower() == 'n':
            print("Skipping sector fix.")
            return
        elif choice.lower() == 'x':
            raise Exception("exit")
    
    try:
        # Run the hdparm repair command
        output = subprocess.check_output(
            ["hdparm", "--yes-i-know-what-i-am-doing", "--repair-sector", str(sector), device],
            stderr=subprocess.STDOUT  # Capture both stdout and stderr
        )
        # Decode and print the output
        decoded_output = output.decode('utf-8')
        print(decoded_output)

        # Log the output using logger with filename
        subprocess.run(["logger", f"[{__file__}] {decoded_output}"])

        # Store the sector as the last corrected sector
        lastsector = sector
    
    except subprocess.CalledProcessError as e:
        # Handle the error condition by capturing the exit code and output
        print(f"Error repairing sector {sector}: Exit code {e.returncode}")
        print(f"Command output: {e.output.decode('utf-8')}")

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
    
    # Ensure we get the latest SMART data and sector information
    try:
        get_smart()
        if sector == 0:
            print("No new pending sectors to fix.")
            break  # Exit if no new sectors are found
    except Exception as e:
        print(f"Error: {str(e)}")
        break

if DEBUG:
    print("Debug mode complete.")
