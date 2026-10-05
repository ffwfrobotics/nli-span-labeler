#!/bin/bash
# Start the E13 labeler. Configure with env vars (see e13_labeler/config.py).
# Binds to 127.0.0.1 by default; set HOST=0.0.0.0 behind an HTTPS proxy.
cd "$(dirname "$0")"
exec uv run python -m e13_labeler serve "$@"
