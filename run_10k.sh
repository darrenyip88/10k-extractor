#!/bin/bash
# 10-K extractor. All data from SEC EDGAR — free, no API key, no quota.
#   ./run_10k.sh AAPL
#   ./run_10k.sh BRK.B --year 2023
#   ./run_10k.sh RY.TO          # TSX: AIF + MD&A + statements instead of a 10-K
cd "$(dirname "$0")/scripts" || exit 1
case "$1" in
  *[.-][Tt][Oo]) exec python3 extract_tsx.py "$@" ;;
  *)          exec python3 extract_10k.py "$@" ;;
esac
