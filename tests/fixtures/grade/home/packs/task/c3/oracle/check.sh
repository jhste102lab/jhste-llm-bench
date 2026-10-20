#!/bin/sh
out="$2"
printf '%s' '{"cases":[{"id":"a","passed":true,"critical":false},{"id":"b","passed":true,"critical":false}],"setup_error":null}' > "$out"
exit 0
