# Demo recording script for VHS (https://github.com/charmbracelet/vhs).
#   brew install vhs   &&   vhs docs/demo.tape      # -> docs/demo.gif
#
# This runs REAL commands against your timer (not staged output). Before recording:
#   - have a working config.json, and press a button on the timer to wake it
#   - BLE calls take ~8-12s, hence the Sleeps below (widen if your link is slow)
# Prefer a hardware-free clip? Swap the control block for the two commented lines at the bottom.

Output docs/demo.gif
Set FontSize 18
Set Width 1000
Set Height 620
Set Padding 20
Set Theme "Dracula"

# --- off-screen setup so the visible commands stay clean ---
Hide
Type "source venv/bin/activate && clear" Enter
Sleep 1s
Show

Type "# Orbit B-Hyve XD — local control over Bluetooth. No cloud. No Wi-Fi hub." Enter
Sleep 1500ms
Type "python cli.py status" Enter
Sleep 12s
Type "python cli.py start 1 300      # open valve 1 for 5 min — confirmed by read-back" Enter
Sleep 14s
Type "python cli.py stop             # close it" Enter
Sleep 14s
Type "# it talked straight to the timer — nothing left your network." Enter
Sleep 2500ms

# --- hardware-free alternative (comment the block above, uncomment these) ---
# Type "python -m pytest test_e2e.py -q     # 152 tests, no device needed" Enter
# Sleep 4s
