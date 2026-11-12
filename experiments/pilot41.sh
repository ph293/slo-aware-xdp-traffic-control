#!/bin/sh
# usage: ./pilot41.sh   (no arguments; ~3.5 min, runs in the foreground)
# pilot41: feasibility pilot for exp41 on the 6 vCPU host VM: B's UDP -b 6G flood through the generic-mode XDP
#          token bucket (offered rate, cap 2500 / 200 Mbit/s holding, tick length, host CPU), V2's ping RTT with
#          and without the flood, and one full exp41 vfrot 0.8_1_2_0.94 slot with all four flooders.
#          Prints PASS/WARN/FAIL per check and an overall verdict at the end.
# writes ONLY pilot41_run.log, pilot41_ts.csv, pilot41_vfrot_ts.csv (never the exp41 outputs); refuses if any of
# them exists, if exp41 or another pilot is running, or unless nproc = 6. Needs tenant1-6 and the 6 iperf3 servers.
cd "$(dirname "$0")"
if pgrep -f "pilot41.py" >/dev/null; then echo "pilot41.py is already running"; exit 1; fi
if pgrep -f "python3 -u exp41.py" >/dev/null; then echo "exp41.py is running; the pilot would disturb it"; exit 1; fi
for f in pilot41_run.log pilot41_ts.csv pilot41_vfrot_ts.csv; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; pilot41 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, pilot41 requires exactly 6 (the exp41 host VM)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
python3 -c "import pilot41; print(pilot41.plan_line())" || exit 1
sudo -v || exit 1   # cache sudo credentials now; pilot41.py uses sudo -n and never prompts
python3 -u pilot41.py 2>&1 | tee pilot41_run.log
