#!/bin/bash
cd "$(dirname "$0")"
python3 app.py &
sleep 2
open http://localhost:5050
