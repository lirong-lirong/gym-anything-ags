#!/bin/bash
set -e
echo "=== Setting up duplicate_regional_slides task ==="

# Source shared utilities
source /workspace/scripts/task_utils.sh

# Record task start time
date +%s > /tmp/task_start_time.txt

# Ensure directories exist
sudo -u ga mkdir -p /home/ga/Documents/Presentations

ODP_PATH="/home/ga/Documents/Presentations/quarterly_review.odp"

# Clean up previous runs
rm -f "$ODP_PATH" 2>/dev/null || true

# Generate the initial presentation directly as ODP. Avoid a PPTX conversion
# step here because headless LibreOffice conversion can be flaky in sandboxes.
python3 << 'PYEOF'
from odf.opendocument import OpenDocumentPresentation
from odf.draw import Page, Frame, TextBox
from odf.text import P

doc = OpenDocumentPresentation()

def add_slide(name, title, bullets):
    page = Page(name=name, masterpagename="Default")
    doc.presentation.addElement(page)

    title_frame = Frame(width="24cm", height="2cm", x="1.5cm", y="1cm")
    page.addElement(title_frame)
    title_box = TextBox()
    title_frame.addElement(title_box)
    title_box.addElement(P(text=title))

    body_frame = Frame(width="24cm", height="12cm", x="2cm", y="4cm")
    page.addElement(body_frame)
    body_box = TextBox()
    body_frame.addElement(body_box)
    for bullet in bullets:
        body_box.addElement(P(text=bullet))

add_slide("Slide1", "Quarterly Sales Overview", [
    "Total Revenue: $4.2M",
    "Year-over-Year Growth: 12%",
    "New Customers: 47",
])
add_slide("Slide2", "Top Performing Products", [
    "Enterprise Suite: $1.8M",
    "Cloud Platform: $1.3M",
    "Support Services: $1.1M",
])
add_slide("Slide3", "Next Quarter Priorities", [
    "Expand into healthcare vertical",
    "Launch partner program",
    "Increase retention rate to 95%",
])

doc.save("/home/ga/Documents/Presentations/quarterly_review.odp")
PYEOF

if [ ! -f "$ODP_PATH" ]; then
    echo "ERROR: Failed to create ODP file"
    exit 1
fi

# Set ownership
chown ga:ga "$ODP_PATH"

# Record initial slide count (should be 3)
echo "3" > /tmp/initial_slide_count.txt

# Launch LibreOffice Impress
echo "Launching LibreOffice Impress..."
su - ga -c "DISPLAY=:1 libreoffice --impress '$ODP_PATH' > /tmp/impress.log 2>&1 &"

# Wait for window
wait_for_window "LibreOffice Impress" 30

# Maximize window
DISPLAY=:1 wmctrl -r "LibreOffice Impress" -b add,maximized_vert,maximized_horz 2>/dev/null || true

# Focus window
wid=$(get_impress_window_id)
if [ -n "$wid" ]; then
    focus_window "$wid"
fi

# Take initial screenshot
DISPLAY=:1 scrot /tmp/task_initial.png 2>/dev/null || true

echo "=== Task setup complete ==="
