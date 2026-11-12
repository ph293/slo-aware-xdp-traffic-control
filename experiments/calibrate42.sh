#!/bin/sh
# usage: ./calibrate42.sh   (no arguments; ~2.3 min, runs in the foreground; run once before ./run42.sh)
# calib42: plant identification + pilot for exp42's lqr_orig on the 6 vCPU host VM: victims V1 + V2 at 9 Gbit/s
#          and all four flooders; xdp_dropfrac.o on the flooders; per-flooder vhost thread CPU every 0.1 s;
#          S: step each flooder 0 -> 0.5 -> 0 (fit tau, gain); M: all flooders at u 0/0.05/0.1/0.2/0.5 (static map,
#          bytes per XDP packet, realized drop fraction, ssh/iperf3 -J with u = 0.5 held); xdp_limit.o back at the end.
#          Prints PASS/WARN/FAIL per check and a verdict; exp42.py and run42.sh refuse to start on FAIL.
# writes ONLY calib42_run.log, calib42_ts.csv, calib42_result.json; refuses if any of them exists, if exp42 or
# another calibration is running, or unless nproc = 6. Needs tenant1-6 and the 6 iperf3 servers.
cd "$(dirname "$0")"
if pgrep -f "calib42.py" >/dev/null; then echo "calib42.py is already running"; exit 1; fi
if pgrep -f "python3 -u exp42.py" >/dev/null; then echo "exp42.py is running; the calibration would disturb it"; exit 1; fi
for f in calib42_run.log calib42_ts.csv calib42_result.json; do
  if [ -e "$f" ]; then echo "REFUSING TO START: $f already exists (move or delete it first; calib42 never appends)"; exit 1; fi
done
n=$(nproc)
if [ "$n" != 6 ]; then echo "REFUSING TO START: nproc = $n, calib42 requires exactly 6 (the exp39-exp42 host VM)"; exit 1; fi
echo "host CPUs: $n"
missing=0
for p in 5201 5202 5203 5204 5205 5206; do
  if ! ss -ltnH | awk '{print $4}' | grep -q ":$p\$"; then echo "no iperf3 server listening on host port $p; start it first: iperf3 -s -D -p $p"; missing=1; fi
done
[ "$missing" = 0 ] || exit 1
for o in xdp_limit.o xdp_dropfrac.o; do
  if [ ! -e "$o" ]; then echo "REFUSING TO START: $o missing (build: clang -O2 -g -target bpf -mcpu=v3 -I/usr/include/aarch64-linux-gnu -c xdp_dropfrac.c -o xdp_dropfrac.o)"; exit 1; fi
done
python3 -c "import calib42; print(calib42.plan_line())" || exit 1
sudo -v || exit 1   # cache sudo credentials now; calib42.py uses sudo -n and never prompts
python3 -u calib42.py 2>&1 | tee -i calib42_run.log   # -i: tee survives Ctrl+C and logs the xdp_limit restore
