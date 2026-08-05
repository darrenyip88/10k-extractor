#!/bin/bash
# 10-K extractor. All data from SEC EDGAR — free, no API key, no quota.
#   ./run_10k.sh AAPL
#   ./run_10k.sh BRK.B --year 2023
cd "$(dirname "$0")/scripts" && python3 extract_10k.py "$@"
