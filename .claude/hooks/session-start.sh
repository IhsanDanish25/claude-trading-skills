#!/bin/bash
set -euo pipefail

# Only enforce this check in Claude Code on the web / cloud sessions.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Domains this repo's skills depend on for live market data.
REQUIRED_DOMAINS=(
  "financialmodelingprep.com"
  "query1.finance.yahoo.com"
)

unreachable=()
for domain in "${REQUIRED_DOMAINS[@]}"; do
  # No -f: an HTTP error status still proves the connection reached the
  # host. Only a curl-level failure (DNS/connect/TLS/timeout) means the
  # sandbox's network policy is blocking the domain.
  if ! curl -sS --max-time 5 -o /dev/null "https://${domain}"; then
    unreachable+=("$domain")
  fi
done

if [ "${#unreachable[@]}" -gt 0 ]; then
  {
    echo "ERROR: required network domain(s) unreachable from this sandbox:"
    for domain in "${unreachable[@]}"; do
      echo "  - $domain"
    done
    echo
    echo "This repo's skills call the Financial Modeling Prep API (financialmodelingprep.com)"
    echo "and Yahoo Finance (query1.finance.yahoo.com) for market data. Open this environment's"
    echo "settings, set Network access to Custom, add the domain(s) above to Allowed domains,"
    echo "then restart the session."
  } >&2
  exit 1
fi

echo "Required network domains reachable: ${REQUIRED_DOMAINS[*]}"
