#!/bin/sh
out="$2"
printf '%s' '{"cases":[],"setup_error":"workspace package escapes repo"}' > "$out"
exit 2
