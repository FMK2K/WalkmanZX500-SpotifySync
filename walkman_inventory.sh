#!/usr/bin/env bash
# Sony Walkman ZX500 (Android 9) audio inventory — paths + tags
# Output: TSV. Tags are read from the file headers (same source W.Music uses).

set -uo pipefail

OUT="${1:-walkman_inventory.tsv}"
HEAD_BYTES="${HEAD_BYTES:-5242880}"   # bytes pulled per file (5 MB covers tags+art for ~all formats)

for c in adb ffprobe jq; do
  command -v "$c" >/dev/null || { echo "missing dep: $c" >&2; exit 1; }
done
adb get-state >/dev/null 2>&1 || { echo "no adb device — check USB + debugging" >&2; exit 1; }

shq()  { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\"'\"'/g")"; }   # quote for device shell
flat() { printf '%s' "$1" | tr '\t\r\n' '   '; }                       # strip TSV-breaking chars

# Locate SD card (UUID-named mount)
SD=$(adb shell 'ls -d /storage/????-???? 2>/dev/null' | tr -d '\r' | head -n1)
ROOTS=("/storage/emulated/0")
[ -n "$SD" ] && ROOTS+=("$SD")

EXTS="-iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.aac' \
-o -iname '*.ogg' -o -iname '*.opus' -o -iname '*.wav' -o -iname '*.aif' \
-o -iname '*.aiff' -o -iname '*.dsf' -o -iname '*.dff' -o -iname '*.wma' \
-o -iname '*.ape' -o -iname '*.alac' -o -iname '*.mqa'"

printf 'location\tpath\tsize\tcodec\tsample_rate\tbits\tchannels\tbit_rate\tduration_s\ttitle\tartist\talbum_artist\talbum\ttrack\tdisc\tyear\tgenre\tcomposer\n' > "$OUT"

for ROOT in "${ROOTS[@]}"; do
  LOC=internal; [ "$ROOT" != "/storage/emulated/0" ] && LOC=sdcard
  echo ">> scanning $ROOT  ($LOC)" >&2

  adb shell "find $(shq "$ROOT") -type f \\( $EXTS \\) 2>/dev/null" \
    | tr -d '\r' \
    | while IFS= read -r p; do
        [ -z "$p" ] && continue

        j=$(adb exec-out "head -c $HEAD_BYTES $(shq "$p")" 2>/dev/null \
              | ffprobe -v error -hide_banner -print_format json \
                        -show_format -show_streams - 2>/dev/null) || j='{}'
        [ -z "$j" ] && j='{}'

        # case-insensitive tag table for this file
        tags=$(jq '.format.tags // {} | with_entries(.key |= ascii_downcase)' <<<"$j")

        get() {  # try aliases in order, return first non-empty
          local k v
          for k in "$@"; do
            v=$(jq -r --arg k "$k" '.[$k] // ""' <<<"$tags")
            [ -n "$v" ] && [ "$v" != "null" ] && { printf '%s' "$v"; return; }
          done
        }

        size=$(jq -r '.format.size // ""'                                                  <<<"$j")
        dur=$( jq -r '.format.duration // ""'                                              <<<"$j")
        br=$(  jq -r '.format.bit_rate // ""'                                              <<<"$j")
        cod=$( jq -r '.streams[0].codec_name // ""'                                        <<<"$j")
        sr=$(  jq -r '.streams[0].sample_rate // ""'                                       <<<"$j")
        bps=$( jq -r '.streams[0].bits_per_raw_sample // .streams[0].bits_per_sample // ""' <<<"$j")
        ch=$(  jq -r '.streams[0].channels // ""'                                          <<<"$j")

        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$LOC" "$(flat "$p")" "$size" "$cod" "$sr" "$bps" "$ch" "$br" "$dur" \
          "$(flat "$(get title)")" \
          "$(flat "$(get artist)")" \
          "$(flat "$(get album_artist albumartist 'album artist')")" \
          "$(flat "$(get album)")" \
          "$(flat "$(get track tracknumber)")" \
          "$(flat "$(get disc discnumber)")" \
          "$(flat "$(get date year originaldate)")" \
          "$(flat "$(get genre)")" \
          "$(flat "$(get composer)")" \
          >> "$OUT"

        echo "  + ${p##*/}" >&2
    done
done

echo "done → $OUT  ($(($(wc -l < "$OUT") - 1)) tracks)" >&2