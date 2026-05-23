#!/bin/bash
cd "$(dirname "$0")"
source ~/.zshrc 2>/dev/null
python3 app.py &
sleep 2
open "http://localhost:8080"
wait
