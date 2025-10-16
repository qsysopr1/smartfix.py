#!/usr/bin/python3
"""
smartfix.py - Cycle through disk errors and repair them.

Enhancements:
- --device /dev/sdX
- --skip-short to bypass initial SMART short self-test
- --select START-END (repeatable) to run selective self-tests
- --assume-yes to skip confirmation prompts (also auto-runs post-repair verify)
- --fix-first-error: when used with --skip-short, ignore pendcount and
  repair the sector listed as LBA_of_first_error (if present)
- --verify-retries N: retry count for interrupted selective verify (default 2)
- --verify-wait SECONDS: wait between verify retries (default 10)
- --start-pause SECONDS: brief pause after issuing a test before status reads (default 0.5)
- --debug for verbose output

Behavior:
- Honors "Self-test routine in progress" by polling -c with remaining%.
- Will not start a short/selective test while one is active (waits with progress).
- After a repair, offers (or auto-runs with --assume-yes) a +/-5 sector selective
  verify; if interrupted/aborted, it retries, then falls back to a short test.
"""

import argparse
import json
import subprocess
import sys
import time

# ----------------------------- Globals ---------------------------------
yn = ""
pendline = ""
sector = 0
lastsector = -1
pendcount = -1
device = "/dev/sda"
DEBUG = False
ASSUME_YES = False
VERIFY_RETRIES = 2
VERIFY_WAIT = 10
START_PAUSE = 0.5  # seconds

# ----------------------------- Helpers ---------------------------------
def debug_print(message):
    if DEBUG:
        print(message)

def print_disclaimer():
    disclaimer = """
*******************************************************
DISCLAIMER:
This script performs low-level disk repairs using hdparm.
Repairing sectors can cause data loss or corruption.

Use at your own risk. Ensure you have current backups.

The author assumes no responsibility for any damage.
*******************************************************
"""
    print(disclaimer)

def run(cmd):
    """Run a command and return (exitcode, stdout_str)."""
    debug_print("RUN: " + " ".join(cmd))
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return 0, out.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode("utf-8", errors="replace")

def smartjson(cmd_extra):
    """Run smartctl with -j and return parsed JSON (or None)."""
    code, out = run(["smartctl", "-j"] + cmd_extra + [device])
    if code != 0:
        debug_print("smartctl non-zero exit {}: {}".format(code, out))
    try:
        return json.loads(out)
    except Exception as e:
        debug_print("JSON parse error: {}".format(e))
        return None

# --------- SMART self-test status / progress handling ----------
def get_selftest_status():
    """
    Query smartctl capabilities/status (-c) and return:
      {
        'in_progress': bool,
        'remaining_percent': int or None,
        'status_string': str or '',
        'raw_value': int or None
      }
    """
    data = smartjson(["-c"])
    result = {"in_progress": False, "remaining_percent": None, "status_string": "", "raw_value": None}
    if not data:
        return result
    try:
        st = data.get("ata_smart_self_test", {}).get("status", {})
        s_str = st.get("string", "") or ""
        rem = st.get("remaining_percent", None)
        val = st.get("value", None)
        in_prog = ("in progress" in s_str.lower()) or (isinstance(val, int) and val == 249)
        result.update({
            "in_progress": bool(in_prog),
            "remaining_percent": rem if isinstance(rem, int) else None,
            "status_string": s_str,
            "raw_value": val if isinstance(val, int) else None
        })
    except Exception:
        pass
    return result

def wait_for_selftest_completion(poll_interval=4):
    """
    Poll -c for 'in progress' with remaining percent; when finished, echo the
    last -l selftest status line for context.
    """
    while True:
        status = get_selftest_status()
        if not status["in_progress"]:
            # Show final status from the self-test log (if available)
            data = smartjson(["-A", "-l", "selftest"])
            if data:
                try:
                    s = data["ata_smart_self_test_log"]["standard"]["table"][0]["status"]["string"]
                    print("Self-test status: {}".format(s))
                except Exception:
                    print("Self-test completed (status not found in log).")
            else:
                print("Self-test completed (unable to read SMART log).")
            break
        rem = status["remaining_percent"]
        if rem is None:
            print("Self-test in progress...")
        else:
            print("Self-test in progress: {}% remaining ...".format(rem))
        time.sleep(poll_interval)

def ensure_no_active_selftest():
    """
    If a self-test is active, wait for it to finish. This prevents overlapping tests
    and ensures selective/short commands are issued only when the drive is ready.
    """
    status = get_selftest_status()
    if status["in_progress"]:
        print("A SMART self-test is already in progress. Waiting for it to complete...")
        wait_for_selftest_completion()

def get_last_selftest_entry():
    """
    Return (status_string, lba_int_or_0) for the most recent self-test log entry.
    """
    data = smartjson(["-A", "-l", "selftest"])
    if not data:
        return ("", 0)
    try:
        entry = data["ata_smart_self_test_log"]["standard"]["table"][0]
        s = entry.get("status", {}).get("string", "")
        lba = entry.get("lba", 0)
        if isinstance(lba, int):
            return (s, lba)
    except Exception:
        pass
    return ("", 0)

# --------- Test runners ----------
def start_short_test():
    """Kick off a short test and wait for it to finish."""
    ensure_no_active_selftest()
    print("Starting SMART short test...")
    _ = run(["smartctl", "-t", "short", device])
    # tiny pause to let controller/firmware settle before first status read
    if START_PAUSE > 0:
        time.sleep(START_PAUSE)
    wait_for_selftest_completion()

def start_selective_tests(ranges):
    """Run one or more selective tests (list of (start, end) tuples)."""
    ensure_no_active_selftest()
    for (start, end) in ranges:
        rng = "{:d}-{:d}".format(start, end)
        print("Starting selective self-test for LBA range {}".format(rng))
        _ = run(["smartctl", "-t", "select,{}".format(rng), device])
        # tiny pause to avoid immediate 'Interrupted' reads on some controllers
        if START_PAUSE > 0:
            time.sleep(START_PAUSE)
        wait_for_selftest_completion()

# --------- SMART data extractors ----------
def refresh_pending_and_failure():
    """
    Update globals: pendcount, sector, pendline.
    - pendcount from attribute 197 (Current_Pending_Sector)
    - sector from last self-test with 'Completed: read failure' (JSON 'lba')
    Returns True if info was refreshed, False otherwise.
    """
    global pendcount, sector, pendline, yn

    data = smartjson(["-A", "-l", "selftest"])
    if not data:
        print("Error: unable to read SMART data")
        return False

    # Current_Pending_Sector (197)
    pending_sector_info = None
    try:
        for attribute in data["ata_smart_attributes"]["table"]:
            if attribute.get("id") == 197:
                pending_sector_info = attribute
                break
    except Exception:
        pass

    if pending_sector_info is None:
        print("Warning: attribute 197 (Current_Pending_Sector) not found")
        pendcount = -1
    else:
        pendcount = pending_sector_info.get("raw", {}).get("value", -1)

    # Look for a self-test showing read failure to capture an LBA
    sector_found = None
    pendline_local = ""
    try:
        for log in data["ata_smart_self_test_log"]["standard"]["table"]:
            if log.get("status", {}).get("string") == "Completed: read failure":
                if "lba" in log and isinstance(log["lba"], int) and log["lba"] > 0:
                    sector_found = int(log["lba"])
                else:
                    sector_found = None
                pendline_local = "Completed: read failure"
                break
    except Exception:
        pass

    if sector_found:
        sector = sector_found
        pendline = pendline_local
        yn = sector
    else:
        if sector == 0:
            yn = "No pending sectors or read failures found"

    if DEBUG:
        debug_print("Detected failed LBA from self-test: {}".format(sector if sector_found else 0))
        debug_print("Pending sector count (197): {}".format(pendcount))
    return True

def fix_sector():
    """Attempt to repair the current 'sector' via hdparm."""
    global sector, lastsector
    if sector == 0:
        raise Exception("Zero sector number")
    if sector == lastsector:
        print("Skipping sector {}: already attempted previously.".format(sector))
        return
    print("Attempting repair for sector {} ...".format(sector))
    code, out = run(["hdparm", "--yes-i-know-what-i-am-doing", "--repair-sector", str(sector), device])
    print(out)
    _ = run(["logger", "[{}] {}".format(__file__, out.strip())])
    lastsector = sector
    if code != 0:
        print("hdparm returned non-zero exit code {}".format(code))

def parse_ranges(select_list):
    """
    Parse ['start-end', 'start2-end2', ...] into list of (start,end) ints.
    Accepts commas inside each entry (ignored) to mimic smartctl syntax.
    """
    ranges = []
    for item in select_list or []:
        item = item.replace(",", "")
        parts = item.split("-")
        if len(parts) != 2:
            raise ValueError("Invalid --select value '{}', expected START-END".format(item))
        try:
            start = int(parts[0], 10)
            end = int(parts[1], 10)
        except ValueError:
            raise ValueError("Non-integer in --select '{}'".format(item))
        if start < 0 or end < 0 or end < start:
            raise ValueError("Invalid range {}-{}".format(start, end))
        ranges.append((start, end))
    return ranges

def confirm(prompt_text):
    """Ask user to confirm; respect --assume-yes."""
    if ASSUME_YES:
        print("{} y (auto)".format(prompt_text))
        return True
    ans = input("{} (y/n): ".format(prompt_text)).strip().lower()
    return ans == "y"

def run_verify_selective_around(lba, delta=5):
    """
    Run a selective test around the given LBA (+/-delta), retry if interrupted,
    then refresh and report.
    """
    start = lba - delta
    if start < 0:
        start = 0
    end = lba + delta

    attempts = 0
    while True:
        attempts += 1
        print("Preparing to run selective verify over LBA range {}-{} ... (attempt {})".format(start, end, attempts))
        start_selective_tests([(start, end)])

        # Inspect latest self-test result
        status_str, lba_from_log = get_last_selftest_entry()
        s_lower = status_str.lower()

        # Handle inconclusive/interrupted outcomes
        if ("interrupted" in s_lower) or ("aborted" in s_lower) or ("host reset" in s_lower):
            if attempts <= VERIFY_RETRIES:
                print("Selective verify was interrupted ('{}'). Retrying in {}s ...".format(status_str, VERIFY_WAIT))
                time.sleep(VERIFY_WAIT)
                continue
            else:
                print("Selective verify repeatedly interrupted ('{}').".format(status_str))
                return "interrupted"

        # Completed outcomes (pass/fail)
        _ = refresh_pending_and_failure()
        if sector == 0:
            print("After selective verify: no failing LBA reported.")
            return "clear"
        else:
            print("After selective verify: first failing LBA reported at {}.".format(sector))
            return "failed"

# ------------------------------ Main -----------------------------------
def main():
    global device, DEBUG, pendcount, sector, ASSUME_YES, VERIFY_RETRIES, VERIFY_WAIT, START_PAUSE

    ap = argparse.ArgumentParser(description="SMART/hdparm helper for detecting and repairing bad sectors.")
    ap.add_argument("-d", "--device", default="/dev/sda", help="Block device (default: /dev/sda)")
    ap.add_argument("--skip-short", action="store_true", help="Skip the initial SMART short self-test")
    ap.add_argument("--select", action="append", default=[],
                    help="Run a selective self-test over START-END (repeatable). Example: --select 849927190-849927211")
    ap.add_argument("--assume-yes", action="store_true", help="Do not prompt for confirmation (auto-yes to offers)")
    ap.add_argument("--fix-first-error", action="store_true",
                    help="When used with --skip-short, ignore pendcount and repair the sector listed as LBA_of_first_error (if present)")
    ap.add_argument("--verify-retries", type=int, default=2, help="Retries for interrupted selective verify (default 2)")
    ap.add_argument("--verify-wait", type=int, default=10, help="Seconds between verify retries (default 10)")
    ap.add_argument("--start-pause", type=float, default=0.5, help="Seconds to pause after starting a test before status reads")
    ap.add_argument("--debug", action="store_true", help="Verbose debug output")
    args = ap.parse_args()

    device = args.device
    DEBUG = args.debug
    ASSUME_YES = args.assume_yes
    VERIFY_RETRIES = max(0, args.verify_retries)
    VERIFY_WAIT = max(1, args.verify_wait)
    START_PAUSE = max(0.0, args.start_pause)

    print_disclaimer()
    print("Device: {}".format(device))

    if not ASSUME_YES:
        cont = input("Continue? (y/n): ").strip().lower()
        if cont != "y":
            sys.exit(0)

    # Optionally run tests requested explicitly
    ranges = parse_ranges(args.select)

    # 1) Short test unless skipped and unless selective ranges were provided
    if not args.skip_short and not ranges:
        start_short_test()
    # 2) Selective tests (can be run with or without the short test)
    if ranges:
        start_selective_tests(ranges)

    # Refresh SMART to get pending sector count and any failing LBA
    if not refresh_pending_and_failure():
        sys.exit(1)

    # One-shot path: skip short + fix-first-error => ignore pendcount and attempt to repair LBA_of_first_error
    if args.skip_short and args.fix_first_error:
        if sector and sector > 0:
            print("Ignoring pendcount due to --fix-first-error with --skip-short.")
            try:
                fix_sector()
            except Exception as e:
                print("Error during repair: {}".format(str(e)))

            # After the one-shot repair, re-check SMART and handle verification
            repaired_lba = lastsector  # the one we just attempted

            _ = refresh_pending_and_failure()
            post_lba = sector
            post_pend = pendcount

            if post_lba == 0:
                print("No further sectors reported as failed in SMART log.")
            elif post_lba == repaired_lba:
                start = repaired_lba - 5 if repaired_lba >= 5 else 0
                end = repaired_lba + 5
                msg = (
                    "SMART self-test log still lists the same LBA {} as the first error. "
                    "This is expected until a new self-test runs.\n"
                    "Run a selective verify now over --select {}-{} ?"
                ).format(repaired_lba, start, end)
                if confirm(msg):
                    outcome = run_verify_selective_around(repaired_lba, 5)
                    if outcome == "interrupted":
                        if confirm("Selective verify kept interrupting. Run a SMART short test now?"):
                            start_short_test()
                            _ = refresh_pending_and_failure()
                            if sector == 0:
                                print("After short test: no failing LBA reported.")
                            else:
                                print("After short test: first failing LBA reported at {}.".format(sector))
                else:
                    if confirm("Run a SMART short test instead now?"):
                        start_short_test()
                        _ = refresh_pending_and_failure()
                        if sector == 0:
                            print("After short test: no failing LBA reported.")
                        else:
                            print("After short test: first failing LBA reported at {}.".format(sector))
                    else:
                        print("Verification skipped at user request.")
            else:
                print(
                    "A different failing LBA is now reported: {} (previously repaired {})."
                    .format(post_lba, repaired_lba)
                )

            if DEBUG:
                st = get_selftest_status()
                print("Post-repair state: LBA={}, pendcount={}, test_in_progress={}, remaining={}".format(
                    post_lba, post_pend, st["in_progress"], st["remaining_percent"]))
            sys.exit(0)
        else:
            print("No LBA_of_first_error reported by SMART; nothing to fix in one-shot mode.")
            sys.exit(0)

    # Default loop: use pendcount to drive repairs if available
    if pendcount == -1:
        raise Exception("Pending sector count is negative or unavailable: {}".format(pendcount))

    print("pendcounter {}".format(pendcount))
    pendcounter = pendcount

    while pendcounter > 0:
        print("{}\t{}\t{}".format(pendcount, pendline, sector))
        if sector != 0:
            try:
                fix_sector()
            except Exception as e:
                print("Error during repair: {}".format(str(e)))
                break
        else:
            print("No specific LBA available to repair; consider running a long or selective test.")
            break

        pendcounter -= 1
        # Re-read SMART after each attempt
        if not refresh_pending_and_failure():
            break
        if sector == 0:
            print("No new pending sectors to fix.")
            break

    if DEBUG:
        print("Debug mode complete.")

if __name__ == "__main__":
    main()
