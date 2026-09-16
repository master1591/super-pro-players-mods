#!/usr/bin/env bash
set -euo pipefail

# Prepare the six SPP release cues supplied and approved by the server owner.
# Raw cue files remain ignored; only the reviewed package archive is released.

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 <super-team-source.mp3> <pro-team-source.mp3> <output-directory>" >&2
  exit 2
fi

super_source=$1
pro_source=$2
output_dir=$3
mkdir -p "$output_dir"

make_clip() {
  local source_file=$1
  local start=$2
  local duration=$3
  local target=$4
  local fade_start=$5
  local fade_duration=$6
  local output_file=$7
  local analysis
  local measured_i measured_lra measured_tp measured_thresh offset

  analysis=$(ffmpeg -hide_banner -nostdin -i "$source_file" \
    -af "atrim=start=${start}:duration=${duration},asetpts=PTS-STARTPTS,loudnorm=I=${target}:LRA=7:TP=-1.5:print_format=json" \
    -f null - 2>&1 | sed -n '/^{/,/^}/p')
  measured_i=$(jq -r '.input_i' <<<"$analysis")
  measured_lra=$(jq -r '.input_lra' <<<"$analysis")
  measured_tp=$(jq -r '.input_tp' <<<"$analysis")
  measured_thresh=$(jq -r '.input_thresh' <<<"$analysis")
  offset=$(jq -r '.target_offset' <<<"$analysis")

  ffmpeg -hide_banner -loglevel error -nostdin -y -i "$source_file" \
    -af "atrim=start=${start}:duration=${duration},asetpts=PTS-STARTPTS,loudnorm=I=${target}:LRA=7:TP=-1.5:measured_I=${measured_i}:measured_LRA=${measured_lra}:measured_TP=${measured_tp}:measured_thresh=${measured_thresh}:offset=${offset}:linear=true,afade=t=in:st=0:d=0.012:curve=qsin,afade=t=out:st=${fade_start}:d=${fade_duration}:curve=qsin" \
    -map_metadata -1 -ar 44100 -ac 2 -c:a libmp3lame -b:a 192k \
    "$output_file"
}

make_clip "$super_source" 6.980 4.000 -18 3.750 0.250 "$output_dir/super_round_1.mp3"
make_clip "$super_source" 13.550 7.000 -16 6.650 0.350 "$output_dir/super_round_2.mp3"
make_clip "$super_source" 21.805 18.000 -14 17.400 0.600 "$output_dir/super_round_3.mp3"

make_clip "$pro_source" 2.000 4.000 -18 3.750 0.250 "$output_dir/pro_round_1.mp3"
make_clip "$pro_source" 9.700 7.000 -16 6.650 0.350 "$output_dir/pro_round_2.mp3"
make_clip "$pro_source" 61.930 18.000 -14 17.400 0.600 "$output_dir/pro_round_3.mp3"

echo "Prepared six SPP release cues in $output_dir"
