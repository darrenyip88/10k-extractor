#!/bin/bash
# 10-K extractor. All data from SEC EDGAR — free, no API key, no quota.
#   ./run_10k.sh AAPL
#   ./run_10k.sh BRK.B --year 2023
#   ./run_10k.sh DOL            # TSX: AIF + MD&A + statements instead of a 10-K
#   ./run_10k.sh TSX:L          # say the exchange when a US ticker has the same letters
# route.py picks the extractor: a 10-K filer goes to extract_10k.py, everything
# Toronto goes to extract_tsx.py.
cd "$(dirname "$0")/scripts" || exit 1
exec python3 route.py "$@"
