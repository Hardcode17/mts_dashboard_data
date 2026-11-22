#!/bin/bash
set -eu
case "$#:${1:-}" in
  0:) action=reset ;;
  1:--check) action=check ;;
  *) echo 'Использование: reset-pi.command [--check]' >&2; exit 2 ;;
esac
exec /bin/bash "$(dirname "$0")/run-pi.sh" "$action"
