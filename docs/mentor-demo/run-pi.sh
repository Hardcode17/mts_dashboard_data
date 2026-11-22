#!/bin/bash
set -euo pipefail
case "${1:-}" in
  reset|send|check) operation=$1 ;;
  *) echo 'Expected check, reset or send' >&2; exit 2 ;;
esac
repo=Hardcode17/mts_dashboard_backend
run=$(gh api --method POST "repos/$repo/actions/workflows/mentor-pi.yml/dispatches" \
  -f ref=main -f "inputs[operation]=$operation" -F return_run_details=true --jq '.workflow_run_id')
[[ "$run" =~ ^[0-9]+$ ]] || { echo 'Проверьте последний запуск Pi mentor demo в GitHub Actions.' >&2; exit 1; }
echo "https://github.com/$repo/actions/runs/$run"
gh run watch "$run" -R "$repo" --interval 3 --exit-status
case "$operation" in
  send) echo 'Крит INC943741270574 опубликован.' ;;
  reset) echo 'Сброс завершён. Доставка INC943741270571 — на координаторах.' ;;
  check) echo 'Состояние Pi проверено.' ;;
esac
