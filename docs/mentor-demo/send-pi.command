#!/bin/bash
set -eu
[[ $# == 0 ]] || { echo 'Использование: send-pi.command' >&2; exit 2; }
exec /bin/bash "$(dirname "$0")/run-pi.sh" send
