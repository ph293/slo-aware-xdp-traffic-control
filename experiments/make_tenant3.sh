#!/usr/bin/env bash
# make_tenant3.sh - build guest VM tenant3 (10.10.0.13) like tenant2, from the base Alpine image.
# Run as your normal user (NOT with sudo); it calls sudo itself where root is needed.
# It refuses to overwrite anything that already exists and never touches tenant1/tenant2.
set -Eeuo pipefail
trap 'echo "ERROR: line $LINENO failed: $BASH_COMMAND -- stopping, nothing further was done." >&2' ERR

NAME=tenant3
IP=10.10.0.13
PREFIX=24
GW=10.10.0.1
DNS=8.8.8.8
MAC=52:54:00:10:00:13          # tenant1=52:54:00:5f:dd:81, tenant2=52:54:00:db:94:50
BRIDGE=br-slice
SIZE=4G                        # tenant1 was resized to 4G, tenant2 is a copy of it

TB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG_DIR=/var/lib/libvirt/images
BASE="$TB/generic_alpine-3.22.2-aarch64-uefi-cloudinit-r0.qcow2"
USERDATA_SRC="$TB/user-data"
PUBKEY="$HOME/.ssh/id_ed25519.pub"
META3="$TB/meta-data3"
USER3="$TB/user-data3"
NET3="$TB/network-config3"
SEED_TMP="$TB/seed3.iso"
DISK="$IMG_DIR/$NAME.qcow2"
SEED="$IMG_DIR/seed3.iso"

run() { printf '+'; printf ' %q' "$@"; printf '\n'; "$@"; }
die() { echo "REFUSING: $*" >&2; exit 1; }
step() { printf '\n=== %s\n' "$*"; }

step "Pre-flight checks (read-only)"
[[ $EUID -ne 0 ]] || die "run as your normal user, not root/sudo (HOME and ~/.ssh must be yours)"
case "$NAME" in tenant1|tenant2) die "NAME must not be tenant1/tenant2" ;; esac
for p in "$DISK" "$SEED"; do
    case "$p" in *tenant1*|*tenant2*|*seed1*|*seed2*) die "path $p belongs to tenant1/tenant2" ;; esac
done
[[ -f "$BASE" ]]         || die "base image not found: $BASE"
[[ -f "$USERDATA_SRC" ]] || die "not found: $USERDATA_SRC"
[[ -f "$PUBKEY" ]]       || die "SSH public key not found: $PUBKEY"
for f in "$META3" "$USER3" "$NET3" "$SEED_TMP"; do
    [[ ! -e "$f" ]] || die "$f already exists (remove it yourself if it is left over from a failed run)"
done
ip link show "$BRIDGE" >/dev/null || die "bridge $BRIDGE not found"
if ip neigh show "$IP" | grep -q lladdr; then die "$IP already appears in the host ARP table"; fi
run sudo -v
if sudo virsh dominfo "$NAME" >/dev/null 2>&1; then die "a domain named $NAME already exists"; fi
for f in "$DISK" "$SEED"; do
    if sudo test -e "$f"; then die "$f already exists"; fi
done
for d in $(sudo virsh list --all --name); do
    if sudo virsh dumpxml "$d" | grep -qi "$MAC"; then die "MAC $MAC already used by domain $d"; fi
done
echo "OK: no $NAME domain, no existing $DISK / $SEED, MAC and IP not in use."

step "1) Copy base Alpine image to $DISK and resize to $SIZE"
run sudo cp -n "$BASE" "$DISK"
run sudo qemu-img resize "$DISK" "$SIZE"
run sudo qemu-img info "$DISK"

step "2) Create cloud-init files meta-data3, user-data3, network-config3"
set -o noclobber   # '>' will fail rather than overwrite
printf 'instance-id: %s\nlocal-hostname: %s\n' "$NAME" "$NAME" > "$META3"
{
    cat "$USERDATA_SRC"
    cat <<EOF
# --- added for $NAME: reproduces what was done by hand inside tenant1/tenant2 ---
ssh_authorized_keys:
  - $(cat "$PUBKEY")
apk_repos:
  alpine_repo:
    version: 'v3.22'
    community_enabled: true
write_files:
  - path: /etc/resolv.conf
    content: |
      nameserver $DNS
packages:
  - iperf3
  - stress-ng
EOF
} > "$USER3"
cat > "$NET3" <<EOF
version: 1
config:
  - type: physical
    name: eth0
    mac_address: '$MAC'
    subnets:
      - type: static
        address: $IP/$PREFIX
        gateway: $GW
EOF
set +o noclobber
for f in "$META3" "$USER3" "$NET3"; do echo "--- $f"; cat "$f"; done

step "3) Build seed ISO $SEED"
run cloud-localds -N "$NET3" "$SEED_TMP" "$USER3" "$META3"
run sudo mv -n "$SEED_TMP" "$SEED"
run sudo ls -l "$DISK" "$SEED"

step "4) virt-install $NAME (same options as tenant2; only name, disk, seed, MAC differ)"
run sudo virt-install --name "$NAME" --memory 512 --vcpus 1 --import \
    --disk "path=$DISK,bus=virtio" \
    --disk "path=$SEED,device=cdrom,bus=scsi" \
    --network "bridge=$BRIDGE,virtualport_type=openvswitch,model=virtio,mac=$MAC" \
    --osinfo detect=on,require=off --graphics none --noautoconsole --virt-type kvm \
    --boot uefi,firmware.feature0.enabled=no,firmware.feature0.name=secure-boot
run sudo virsh list --all

step "Done"
cat <<EOF
Created: $DISK, $SEED, $META3, $USER3, $NET3, and libvirt domain $NAME (MAC $MAC).
First boot installs iperf3 and stress-ng from the internet, so allow 1-2 minutes, then check:
  sudo virsh console $NAME          (exit with Ctrl+])
  ssh alpine@$IP 'hostname; ip -4 addr show eth0; cat /etc/resolv.conf; apk info | grep -E "iperf3|stress-ng"; doas -n true && echo doas-ok'
EOF
