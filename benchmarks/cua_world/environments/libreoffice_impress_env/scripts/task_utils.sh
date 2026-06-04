#!/bin/bash
# Shared utilities for LibreOffice Impress task setup and export scripts
# NOTE: Do NOT use set -euo pipefail in this file - it is sourced by other scripts

# Wait for a window with specified title to appear
# Args: $1 - window title pattern (grep pattern)
#       $2 - timeout in seconds (default: 60)
# Returns: 0 if found, 1 if timeout
wait_for_window() {
    local window_pattern="$1"
    local timeout=${2:-60}
    local start=$(date +%s)

    echo "Waiting for window matching '$window_pattern'..."

    while true; do
        local elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "Timeout: Window not found after ${timeout}s"
            return 1
        fi
        if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi "$window_pattern"; then
            echo "Window found after ${elapsed}s"
            return 0
        fi
        if echo "$window_pattern" | grep -qi 'impress\|libreoffice\|\.odp\|\.pptx'; then
            if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'impress\|libreoffice\|\.odp\|\.pptx'; then
                echo "LibreOffice window found after ${elapsed}s"
                return 0
            fi
        fi
        if echo "$window_pattern" | grep -qi 'visual studio code\|code'; then
            if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'visual studio code\|code' || pgrep -u ga -f 'code' >/dev/null 2>&1; then
                echo "VSCode process/window found after ${elapsed}s"
                return 0
            fi
        fi
        if echo "$window_pattern" | grep -qi 'thunderbird'; then
            if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'thunderbird'; then
                echo "Thunderbird window found after ${elapsed}s"
                return 0
            fi
        fi
        sleep 1
    done
}

# Wait for a file to be created or modified
# Args: $1 - file path
#       $2 - timeout in seconds (default: 10)
# Returns: 0 if file exists, 1 if timeout
wait_for_file() {
    local filepath="$1"
    local timeout=${2:-10}
    local start=$(date +%s)

    echo "Waiting for file: $filepath"

    while true; do
        local elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "Timeout: File not ready: $filepath"
            return 1
        fi
        if [ -f "$filepath" ]; then
            echo "File ready: $filepath"
            return 0
        fi
        sleep 0.5
    done
}

# Wait for a process to start
# Args: $1 - process name pattern (pgrep pattern)
#       $2 - timeout in seconds (default: 30)
# Returns: 0 if process found, 1 if timeout
wait_for_process() {
    local process_pattern="$1"
    local timeout=${2:-30}
    local start=$(date +%s)

    echo "Waiting for process matching '$process_pattern'..."

    while true; do
        local elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "Timeout: Process not found after ${timeout}s"
            return 1
        fi
        if pgrep -fc "$process_pattern" > /dev/null 2>&1; then
            echo "Process found after ${elapsed}s"
            return 0
        fi
        sleep 1
    done
}

# Focus a window by name pattern
# Args: $1 - window name pattern
# Returns: 0 if focused successfully, 1 otherwise
focus_window() {
    local pattern="$1"
    DISPLAY=:1 wmctrl -a "$pattern" 2>/dev/null && return 0

    # Try by window ID
    local wid
    wid=$(DISPLAY=:1 wmctrl -l | grep -i "$pattern" | head -1 | awk '{print $1}')
    if [ -n "$wid" ]; then
        DISPLAY=:1 wmctrl -ia "$wid" 2>/dev/null && return 0
    fi

    echo "Warning: Failed to focus window: $pattern"
    return 1
}

# Maximize a window by name pattern
# Args: $1 - window name pattern
maximize_window() {
    local pattern="$1"
    DISPLAY=:1 wmctrl -r "$pattern" -b add,maximized_vert,maximized_horz 2>/dev/null || true
}

# Get the window ID for LibreOffice Impress
# Returns: window ID or empty string
get_impress_window_id() {
    DISPLAY=:1 wmctrl -l 2>/dev/null | grep -i 'impress\|libreoffice\|\.odp\|\.pptx' | head -1 | awk '{print $1}'
}

# Kill all LibreOffice processes
kill_libreoffice() {
    pkill -f "soffice" 2>/dev/null || true
    sleep 1
    pkill -9 -f "soffice" 2>/dev/null || true
    sleep 1
}

# Run xdotool command as specified user with DISPLAY set
# Args: $1 - user (e.g., "ga")
#       $2 - display (e.g., ":1")
#       rest - xdotool arguments
safe_xdotool() {
    local user="$1"
    local display="$2"
    shift 2
    su - "$user" -c "DISPLAY=$display xdotool $*" 2>/dev/null || true
}

take_screenshot() {
    local output_file="${1:-/tmp/screenshot.png}"
    DISPLAY=:1 scrot "$output_file" 2>/dev/null || \
    DISPLAY=:1 import -window root "$output_file" 2>/dev/null || \
    echo "Warning: Could not take screenshot"
    [ -f "$output_file" ] && echo "Screenshot saved: $output_file"
}

create_basic_impress_odp() {
    local output_file="$1"
    local template_kind="${2:-generic}"
    mkdir -p "$(dirname "$output_file")"
    python3 - "$output_file" "$template_kind" <<'PYEOF'
import sys
from odf.opendocument import OpenDocumentPresentation
from odf.draw import Page, Frame, TextBox
from odf.text import P

output_file, template_kind = sys.argv[1], sys.argv[2]
doc = OpenDocumentPresentation()

def add_text(page, text, x, y, width="24cm", height="3cm"):
    frame = Frame(width=width, height=height, x=x, y=y)
    page.addElement(frame)
    box = TextBox()
    frame.addElement(box)
    box.addElement(P(text=text))

def add_slide(name, title, lines):
    page = Page(name=name, masterpagename="Default")
    doc.presentation.addElement(page)
    add_text(page, title, "1.5cm", "1cm", height="2cm")
    y = 3.2
    for line in lines:
        add_text(page, line, "2cm", f"{y}cm", height="1.2cm")
        y += 1.4

if template_kind == "renewable":
    slides = [
        ("Slide1", "Renewable Energy Report", ["Global renewable capacity overview", "Solar, wind, hydro, and storage trends"]),
        ("Slide2", "Solar Power", ["Installed solar capacity is expanding", "Grid integration requires storage planning"]),
        ("Slide3", "Wind Power", ["Onshore and offshore wind remain key resources", "Transmission planning is a major constraint"]),
        ("Slide4", "Energy Storage", ["Battery deployment supports peak balancing", "Long-duration storage remains strategic"]),
        ("Slide5", "Policy Outlook", ["Incentives accelerate deployment", "Workforce and supply chain planning matter"]),
    ]
elif template_kind == "workforce":
    slides = [
        ("Slide1", "Health Workforce Report", ["The health workforce supports primary care access", "workforce workforce workforce workforce workforce"]),
        ("Slide2", "Regional Workforce Gaps", ["Rural workforce shortages remain persistent", "workforce workforce workforce workforce workforce workforce"]),
        ("Slide3", "Training Pipeline", ["The workforce pipeline needs retention incentives", "workforce workforce workforce workforce workforce"]),
        ("Slide4", "Planning Priorities", ["Workforce planning connects demand and capacity", "workforce workforce workforce workforce workforce workforce"]),
        ("Slide5", "Recommendations", ["Strengthen the workforce data system", "workforce workforce workforce workforce workforce workforce"]),
    ]
elif template_kind == "flowchart":
    slides = [
        ("Slide1", "Software Development Lifecycle Flowchart", [
            "Create a flowchart using ovals, rectangles, diamonds, and connectors.",
            "Required stages: Start, Requirements, Design, Implementation, Test, Release.",
        ]),
    ]
else:
    slides = [
        ("Slide1", "Starter Presentation", ["Generated fallback presentation asset."]),
    ]

for slide in slides:
    add_slide(*slide)

doc.save(output_file)
PYEOF
    chown ga:ga "$output_file" 2>/dev/null || true
}

# Dismiss all LibreOffice startup dialogs (Recovery, Template, What's New)
dismiss_dialogs() {
    for attempt in 1 2 3; do
        if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi "Recovery\|Template\|What"; then
            echo "Dismissing dialog (attempt $attempt)..."
            su - ga -c "DISPLAY=:1 xdotool key Escape" 2>/dev/null || true
            sleep 2
        else
            break
        fi
    done
    # Extra Escape to dismiss any remaining popups/infobars
    su - ga -c "DISPLAY=:1 xdotool key Escape" 2>/dev/null || true
    sleep 1
}

export -f wait_for_window
export -f wait_for_file
export -f wait_for_process
export -f focus_window
export -f maximize_window
export -f get_impress_window_id
export -f kill_libreoffice
export -f safe_xdotool
export -f take_screenshot
export -f create_basic_impress_odp
export -f dismiss_dialogs
