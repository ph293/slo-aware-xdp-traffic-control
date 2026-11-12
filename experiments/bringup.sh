#!/bin/bash
# restore the testbed after a VM reboot: bridge IP, 6 guests, 6 iperf3 servers
sudo -v || exit 1
ip -br addr show br-slice | grep -q '10.10.0.1/24' || sudo ip addr add 10.10.0.1/24 dev br-slice
sudo ip link set br-slice up
for i in 1 2 3 4 5 6; do sudo virsh -c qemu:///system start tenant$i 2>&1 | tail -1; done
echo "waiting 60 s for the guests to boot..."; sleep 60
for ip in 11 12 13 14 15 16; do ping -c1 -W2 10.10.0.$ip >/dev/null && echo "10.10.0.$ip up" || echo "10.10.0.$ip DOWN"; done
for p in 5201 5202 5203 5204 5205 5206; do ss -ltn | grep -q ":$p " || iperf3 -s -D -p $p; done
echo "iperf3 servers listening: $(ss -ltn | grep -cE ':520[1-6] ')"
echo "ip_forward: $(cat /proc/sys/net/ipv4/ip_forward)"; nproc; free -h | head -2
