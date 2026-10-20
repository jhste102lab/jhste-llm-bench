#!/bin/sh
repo="$1"
out="$2"
printf '%s' '{"cases":[{"id":"ok","passed":true,"critical":false},{"id":"sec","passed":false,"critical":true},{"id":"other","passed":true,"critical":false}],"setup_error":null}' > "$out"
exit 0
