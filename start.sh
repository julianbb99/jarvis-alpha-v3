#!/bin/bash
echo "JARVIS Watchdog gestartet - $(date -u)"
RESTARTS=0
while true; do
    echo "Bot Start #$RESTARTS - $(date -u)"
    python -u bot.py
    EXIT=$?
    RESTARTS=$((RESTARTS + 1))
    echo "Bot beendet (Code: $EXIT) - Restart in 5s... (#$RESTARTS)"
    sleep 5
done
