#!/usr/bin/env bash
# Ежедневный запуск выгрузки (вызывается из cron). Лог — в log.txt в корне проекта.
cd "$(dirname "$0")/.." || exit 1
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" run.py "$@"
