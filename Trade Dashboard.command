#!/bin/bash
# Double-click this file to open the trade dashboard in your browser.
# It launches Streamlit (which opens the browser automatically) and stays
# running in this window. Close the window or press Ctrl+C to stop it.
cd "$(dirname "$0")" || { echo "Could not find the Trade Recorder folder"; read -r; exit 1; }
echo "Starting the trade dashboard..."
echo "Your browser will open shortly. Leave this window open while you use it."
echo "To stop: close this window or press Ctrl+C."
echo ""
exec ./venv/bin/streamlit run app.py
