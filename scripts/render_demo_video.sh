#!/usr/bin/env bash
# Render a captioned, 58-second product demo from the synthetic dashboard.
#
# Usage:
#   bash scripts/render_demo_video.sh [output.mp4]
#
# The dashboard data is intentionally synthetic and carries its own on-screen
# notice. This makes the video reproducible without credentials while avoiding
# a claim that the scripted recovery is live evidence.
set -euo pipefail

output_path=${1:-demo/dr-browser-demo.mp4}
project_root=$(cd "$(dirname "$0")/.." && pwd)
work_dir=$(mktemp -d)
font=/usr/share/fonts/truetype/lato/Lato-Semibold.ttf

cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

mkdir -p "$(dirname "$output_path")"
cd "$project_root"

uv run python scripts/seed_demo.py --db "$work_dir/demo.db" --output "$work_dir/dashboard.html"

# Capture one complete page, then crop the stable dashboard bands. Chromium's
# headless screenshot command races fragment navigation on local files, which
# can leave an anchored capture blank; a full-page capture avoids that race.
playwright screenshot --browser chromium --channel chrome --color-scheme dark \
  --viewport-size '1920,1080' --full-page --wait-for-timeout 500 \
  "file://$work_dir/dashboard.html" "$work_dir/dashboard.png" >/dev/null

crop_band() {
  local name=$1
  local top=$2
  ffmpeg -y -i "$work_dir/dashboard.png" -vf "crop=1920:1080:0:$top" \
    -frames:v 1 "$work_dir/$name.png" -loglevel error
}

crop_band hero 0
crop_band fleet 1200
crop_band recovery 2200
crop_band contracts 5550

ffmpeg -y -f lavfi -i color=c=0b1012:s=1920x1080:d=6 \
  -vf "drawtext=fontfile=$font:text='DR BROWSER':fontcolor=57d8b5:fontsize=38:x=(w-text_w)/2:y=390,drawtext=fontfile=$font:text='Self-healing changelog intelligence':fontcolor=edf4f1:fontsize=68:x=(w-text_w)/2:y=460,drawtext=fontfile=$font:text='A scraper is only trustworthy when its output still meets its contract.':fontcolor=a9bbb6:fontsize=28:x=(w-text_w)/2:y=560" \
  -r 30 -pix_fmt yuv420p "$work_dir/title.mp4" -loglevel error

render_slide() {
  local image=$1
  local duration=$2
  local output_name=$3
  local caption=$4
  ffmpeg -y -loop 1 -i "$image" -t "$duration" \
    -vf "scale=1920:1080,drawbox=x=0:y=946:w=1920:h=134:color=0b1012@0.94:t=fill,drawtext=fontfile=$font:text='$caption':fontcolor=edf4f1:fontsize=30:x=76:y=995" \
    -r 30 -pix_fmt yuv420p "$work_dir/$output_name.mp4" -loglevel error
}

render_slide "$work_dir/hero.png" 13 detect \
  '1 / DETECT  A selector can drift silently while the scraper still returns rows.'
render_slide "$work_dir/fleet.png" 13 check \
  '2 / CHECK  Every field is measured against an explicit quality floor.'
render_slide "$work_dir/recovery.png" 13 heal \
  '3 / HEAL  The contract writes an English symptom for Bright Data Scraper Studio.'
render_slide "$work_dir/contracts.png" 13 verify \
  '4 / VERIFY  A repair earns trust only after fresh output passes the same contract.'

ffmpeg -y \
  -i "$work_dir/title.mp4" \
  -i "$work_dir/detect.mp4" \
  -i "$work_dir/check.mp4" \
  -i "$work_dir/heal.mp4" \
  -i "$work_dir/verify.mp4" \
  -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
  -filter_complex "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0[video]" \
  -map "[video]" -map 5:a -t 58 -c:v libx264 -crf 20 -preset medium -c:a aac -b:a 128k \
  -movflags +faststart "$output_path" -loglevel error

echo "wrote $output_path"
