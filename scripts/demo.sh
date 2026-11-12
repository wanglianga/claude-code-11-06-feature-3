#!/usr/bin/env bash
# End-to-end local demo against the fake four-platform server.
set -euo pipefail
cd "$(dirname "$0")/.."

export CARSCOUT_DATABASE_URL="${CARSCOUT_DATABASE_URL:-postgresql://carscout:carscout@127.0.0.1:5432/carscout}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export FIXTURE_BASE_URL="${FIXTURE_BASE_URL:-http://127.0.0.1:8088}"
export DATA_DIR="${DATA_DIR:-./data}"

if [ -z "${LD_LIBRARY_PATH:-}" ] && [ -d /tmp/chromelibs/usr/lib/x86_64-linux-gnu ]; then
  export LD_LIBRARY_PATH=/tmp/chromelibs/usr/lib/x86_64-linux-gnu  # dev sandbox only
fi
echo "== init-db"; carscout init-db >/dev/null
echo "== fixture"; carscout fixture --port 8088 >/tmp/carscout-fixture.log 2>&1 &
FIXTURE_PID=$!
trap 'kill $FIXTURE_PID 2>/dev/null || true' EXIT
sleep 1

echo "== run (A/B/C/D)"; carscout run --date 2026-11-12
echo "== status"; carscout status | head -40
echo "== export 凯美瑞"; carscout export --series 凯美瑞 --days 30
echo "== review export"; carscout review --export --out /tmp/review_queue.csv
echo "   -> edit /tmp/review_queue.csv (decision=merge|split) then:"
echo "   carscout review --import-file /tmp/review_queue.csv"

echo "== versioned merge rules (config / replay / gray rollout / explain)"
carscout rules list
carscout rules default-spec > /tmp/rule-v2.json
python3 - "$@" <<'PY'
import json, sys
path = "/tmp/rule-v2.json"
spec = json.load(open(path, encoding="utf-8"))
spec["thresholds"] = {"auto_merge": 0.80, "gray_zone": 0.58}
spec["required_evidence"] = ["vin_match"]
json.dump(spec, open(path, "w", encoding="utf-8"), ensure_ascii=False)
PY
carscout rules create --name "VIN必要条件演示版" --spec-file /tmp/rule-v2.json --changelog "demo" || true
NEW_ID=$(carscout rules list | python3 -c "import json,sys; print(max(v['id'] for v in json.load(sys.stdin)))")
echo "   -> offline replay of rule version ${NEW_ID} (read-only impact estimate):"
carscout rules replay --version-id "${NEW_ID}"
echo "   -> evidence-level explanation (pick two listing ids from /api/vehicles):"
echo "   carscout rules explain --a <listing_id> --b <listing_id> [--version-id ${NEW_ID}]"
echo "   -> activate at 10% on platform A after approval when the replay exceeds thresholds:"
echo "   carscout rules approve --replay-id <id>   # only if exceeds_threshold=true"
echo "   carscout rules activate --version-id ${NEW_ID} --rollout A=10"
echo "   carscout rules rollback --reason 'demo rollback'"
