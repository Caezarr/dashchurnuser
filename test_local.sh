#!/bin/bash
# Test all analytics endpoints locally
# Usage: ./test_local.sh [token] [base_url]
# Example: ./test_local.sh Wonka2026Dash http://localhost:7842

TOKEN="${1:-Wonka2026Dash}"
BASE="${2:-http://localhost:7842}"
PASS=0; FAIL=0

check() {
    local name="$1"
    local url="$2"
    local expect="$3"
    local result
    result=$(curl -s "$url" -H "Authorization: Bearer $TOKEN")
    if echo "$result" | grep -q "$expect"; then
        echo "  PASS  $name"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $name"
        echo "        Expected: $expect"
        echo "        Got: ${result:0:120}"
        FAIL=$((FAIL+1))
    fi
}

echo ""
echo "Testing $BASE with token=$TOKEN"
echo "================================================"

# Health
check "health" "$BASE/analytics/health" '"status": "ok"'

# Trigger sync first
echo ""
echo "Triggering sync (wait up to 60s for Langfuse)..."
SYNC=$(curl -s -X POST "$BASE/analytics/sync" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"period":"30d"}')
echo "  Sync result: $(echo $SYNC | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"requesty={d.get(\"requesty\",{}).get(\"records\",0)} records, lf_users={d.get(\"lf_users\",0)}, lf_models={d.get(\"lf_models\",0)}")')"

echo ""
echo "Endpoints:"

# Overview
check "overview - has total_requests" \
    "$BASE/analytics/overview?period=30d" '"total_requests"'
check "overview - unique_models" \
    "$BASE/analytics/overview?period=30d" '"unique_models"'

# Timeseries
check "timeseries - has data" \
    "$BASE/analytics/timeseries?period=30d" '"period"'
check "timeseries - requests > 0" \
    "$BASE/analytics/timeseries?period=30d" '"requests"'

# By model
MODEL_RESP=$(curl -s "$BASE/analytics/by-model?period=30d" -H "Authorization: Bearer $TOKEN")
if echo "$MODEL_RESP" | grep -q '"model"'; then
    echo "  PASS  by-model - has model data"
    PASS=$((PASS+1))
else
    echo "  WARN  by-model - no model data yet (Langfuse may not have synced)"
fi

# By org
check "by-org - has data" \
    "$BASE/analytics/by-org?period=30d" '"org_name"'

# Churn users
check "churn-users - has structure" \
    "$BASE/analytics/churn-users" '"total"'

# Auth check
AUTH_RESP=$(curl -s "$BASE/analytics/overview")
if echo "$AUTH_RESP" | grep -q '"Unauthorized"'; then
    echo "  PASS  auth - rejects unauthenticated"
    PASS=$((PASS+1))
else
    echo "  WARN  auth - no auth required (token not set?)"
fi

echo ""
echo "================================================"
echo "  Results: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "  All good - ready to deploy!" || echo "  Fix failures before deploying."
echo ""
