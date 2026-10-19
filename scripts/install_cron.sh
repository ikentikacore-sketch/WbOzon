#!/usr/bin/env bash
# Ставит задание cron: каждый день в 06:00 по времени компьютера.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LINE="0 6 * * * $DIR/scripts/run_daily.sh >> $DIR/data/cron.out 2>&1"
( crontab -l 2>/dev/null | grep -v "scripts/run_daily.sh"; echo "$LINE" ) | crontab -
echo "Готово. Текущий crontab:"; crontab -l
