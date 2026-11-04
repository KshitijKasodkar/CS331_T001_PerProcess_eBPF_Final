#!/usr/bin/env bash
# Everything for the final evaluation in one go (Raspberry Pi 5, ~1.5 h).
#
#   sudo ./experiments/run_final.sh 10.58.105.150        # IP of the laptop running trafficgen server
#   sudo ./experiments/run_final.sh 10.58.105.150 quick  # ~20 min smoke version
#   sudo ./experiments/run_final.sh local quick          # one machine, loopback only (testing)
#
# 1. proof suite (correctness, performance, scalability, reliability incl. 30 min soak)
# 2. loss experiment 0/1/2/5/10 %, repeated 3 times (+ rcvbuf test once)
# 3. the same with 20 ms extra delay (once)
# 4. combine repeats into mean ± sd, zip everything into ~/final_results.zip
set -u
PEER="${1:?usage: sudo $0 <peer-ip> [quick]}"
QUICK="${2:-}"
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
STAMP=$(date +%Y%m%d-%H%M)
OUT=experiments/results/final_$STAMP
mkdir -p "$OUT"
exec > >(tee -a "$OUT/run_final.log") 2>&1
echo "== run_final $STAMP  peer=$PEER  quick=${QUICK:-no}  host=$(hostname)  kernel=$(uname -r)"

echo; echo "== checking the peer ($PEER:5300) =="
if [ "$PEER" != local ] && ! timeout 5 bash -c "</dev/tcp/$PEER/5300" 2>/dev/null; then
    echo "!! cannot reach $PEER:5300 - start 'py trafficgen.py server --port 5300' on the laptop first"; exit 1
fi
echo "peer OK"

if [ -n "$QUICK" ]; then
    python3 experiments/proof_suite.py --quick --soak-min 2 --out "$OUT"
    REPEATS=1; LOSS="0,2,10"
else
    python3 experiments/proof_suite.py --out "$OUT"
    REPEATS=3; LOSS="0,1,2,5,10"
fi

LOSSDIRS=()
for i in $(seq 1 $REPEATS); do
    echo; echo "== loss experiment run $i/$REPEATS =="
    EXTRA=""; [ "$i" = 1 ] && EXTRA="--rcvbuf-test"
    python3 experiments/run_experiments.py --peer "$PEER" --loss "$LOSS" --out "$OUT/loss_run$i" $EXTRA
    LOSSDIRS+=("$(ls -d "$OUT"/loss_run$i/*/ | head -1)")
done

echo; echo "== loss experiment with 20 ms extra delay =="
python3 experiments/run_experiments.py --peer "$PEER" --loss "$LOSS" --delay 20 --modes tcp-up,tcp-down --out "$OUT/delay20"

python3 experiments/combine_runs.py "${LOSSDIRS[@]}" --out "$OUT/loss_combined" > /dev/null
python3 -m bwtracker doctor > "$OUT/doctor.txt" 2>&1
chown -R "${SUDO_USER:-root}" "$OUT"
HOME_DIR=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
rm -f "$HOME_DIR/final_results.zip"; (cd experiments/results && python3 -m zipfile -c "$HOME_DIR/final_results.zip" "final_$STAMP")
chown "${SUDO_USER:-root}" "$HOME_DIR/final_results.zip"
echo; echo "== DONE. Results: $OUT   Zip: $HOME_DIR/final_results.zip"
cat "$OUT"/proof_*/summary.md | head -40
