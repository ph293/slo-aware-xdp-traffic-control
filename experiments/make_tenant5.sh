#!/usr/bin/env bash
# make_tenant5.sh - build guest VM tenant5 (10.10.0.15) as a clone of tenant3.
# Run as: sudo bash make_tenant5.sh
#
# Disk:   fresh copy of the base Alpine cloud image (exactly how tenant3.qcow2 was made:
#         base copy + resize 4G). tenant3.qcow2 is never read beyond a header-only
#         'qemu-img info -U', because copying a running guest's disk is not crash-consistent.
# Seed:   cloud-init NoCloud ISO with tenant3's user-data (same password, SSH key, packages,
#         resolv.conf), new instance-id/hostname tenant5, static 10.10.0.15/24 gw 10.10.0.1.
# Domain: 'virsh dumpxml --inactive tenant3', edited (name, uuid, MAC, disk, seed, nvram,
#         OVS interfaceid removed, 1 vCPU, 524288 KiB), then define + start.
# Host tap: libvirt names it vnetN itself (the 'vnet' prefix is reserved, a <target dev='vnet..'/> is
#         ignored), so it is not pinned; expected vnet4 when tenant1-4 are running and
#         tenant5 is created first. exp38.py never relies on the name (virsh domiflist).
# It never stops/modifies tenant1-4, br-slice's IP, OVS config of existing ports, or XDP programs.
set -Eeuo pipefail
trap 'echo "ERROR: line $LINENO failed: $BASH_COMMAND -- stopping." >&2' ERR

NAME=tenant5
SRC=tenant3
IP=10.10.0.15
PREFIX=24
GW=10.10.0.1
MAC=52:54:00:10:00:15          # tenant3=52:54:00:10:00:13, same scheme
BRIDGE=br-slice
SIZE=4G                        # same as tenant1-3
VCPUS=1
MEM_KIB=524288
EXPECT_DEV=vnet4               # informational only (see header)

TB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG_DIR=/var/lib/libvirt/images
NVRAM_DIR=/var/lib/libvirt/qemu/nvram
BASE="$TB/generic_alpine-3.22.2-aarch64-uefi-cloudinit-r0.qcow2"
USER_SRC="$TB/user-data3"                  # source of tenant3's seed3.iso user-data
META5="$TB/meta-data5"
USER5="$TB/user-data5"
NET5="$TB/network-config5"
DISK="$IMG_DIR/$NAME.qcow2"
SEED="$IMG_DIR/seed5.iso"
SRC_SEED="$IMG_DIR/seed3.iso"
NVRAM="$NVRAM_DIR/${NAME}_VARS.fd"
OWNER="${SUDO_USER:-}"

run() { printf '+'; printf ' %q' "$@"; printf '\n'; "$@"; }
die() { echo "REFUSING: $*" >&2; exit 1; }
step() { printf '\n=== %s\n' "$*"; }

step "Pre-flight checks (read-only)"
[[ $EUID -eq 0 ]] || die "run with: sudo bash $0"
[[ -n "$OWNER" && "$OWNER" != root ]] || die "run via sudo from your normal user (SUDO_USER is used to chown files in $TB)"
for t in cloud-localds qemu-img virsh ovs-vsctl python3 isoinfo ping; do
    command -v "$t" >/dev/null || die "required tool not found: $t"
done
if virsh dominfo "$NAME" >/dev/null 2>&1; then die "a domain named $NAME already exists"; fi
for f in "$DISK" "$SEED" "$NVRAM" "$META5" "$USER5" "$NET5"; do
    [[ ! -e "$f" ]] || die "$f already exists (remove it yourself if it is left over from a failed run)"
done
compgen -G "$IMG_DIR/*tenant5*" >/dev/null && die "a file matching $IMG_DIR/*tenant5* already exists"
[[ -f "$BASE" ]]     || die "base image not found: $BASE"
[[ -f "$USER_SRC" ]] || die "not found: $USER_SRC"
[[ -f "$SRC_SEED" ]] || die "not found: $SRC_SEED"
virsh dominfo "$SRC" >/dev/null || die "source domain $SRC not found"
ip link show "$BRIDGE" >/dev/null || die "bridge $BRIDGE not found"
if ip neigh show "$IP" | grep -q lladdr; then die "$IP already appears in the host ARP table"; fi
if ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then die "$IP already answers ping"; fi
for d in $(virsh list --all --name); do
    if virsh dumpxml --inactive "$d" | grep -qi "$MAC"; then die "MAC $MAC already used by domain $d"; fi
done
if ip -br link | grep -qi "fe:54:00${MAC#52:54:00}"; then
    die "a host tap with MAC fe:54:00${MAC#52:54:00} already exists"
fi

# tenant3's seed must really have been built from user-data3 (so the clone gets the same user/key)
SEED3_UD="$(isoinfo -R -i "$SRC_SEED" -x /user-data 2>/dev/null || isoinfo -J -i "$SRC_SEED" -x /USER_DATA 2>/dev/null || true)"
[[ -n "$SEED3_UD" ]] || die "could not read user-data from $SRC_SEED"
if ! diff <(printf '%s\n' "$SEED3_UD") "$USER_SRC" >/dev/null; then
    diff <(printf '%s\n' "$SEED3_UD") "$USER_SRC" || true
    die "$SRC_SEED user-data differs from $USER_SRC (see diff above); not guessing which is right"
fi
SRC_VSIZE="$(qemu-img info -U --output=json "$IMG_DIR/$SRC.qcow2" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')"
echo "tenant3 disk virtual size: $SRC_VSIZE bytes (header read only, -U, no lock taken)"
echo "OK: no $NAME domain/files, MAC $MAC and IP $IP unused, seed3 user-data == $USER_SRC."

step "1) Fresh disk $DISK from base image (tenant3's disk is NOT copied; it is running)"
run qemu-img convert -O qcow2 "$BASE" "$DISK"
run qemu-img resize "$DISK" "$SIZE"
run qemu-img info "$DISK"
NEW_VSIZE="$(qemu-img info --output=json "$DISK" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')"
[[ "$NEW_VSIZE" == "$SRC_VSIZE" ]] || echo "WARNING: $DISK size $NEW_VSIZE != tenant3 size $SRC_VSIZE"

step "2) cloud-init files meta-data5, user-data5, network-config5 (in $TB)"
set -o noclobber
printf 'instance-id: %s\nlocal-hostname: %s\n' "$NAME" "$NAME" > "$META5"
sed "s/added for $SRC:/added for $NAME:/" "$USER_SRC" > "$USER5"
cat > "$NET5" <<EOF
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
chown "$OWNER:" "$META5" "$USER5" "$NET5"
for f in "$META5" "$USER5" "$NET5"; do echo "--- $f"; cat "$f"; done

step "3) Seed ISO $SEED"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
run cloud-localds -N "$NET5" "$WORK/seed5.iso" "$USER5" "$META5"
run install -m 0644 "$WORK/seed5.iso" "$SEED"
run ls -l "$DISK" "$SEED"

step "4) Domain XML from 'virsh dumpxml --inactive $SRC'"
virsh dumpxml --inactive "$SRC" > "$WORK/$SRC.xml"
SRC_UUID="$(virsh domuuid "$SRC")"
python3 -I - "$WORK/$SRC.xml" "$WORK/$NAME.xml" <<PY
import sys, xml.etree.ElementTree as ET
src, dst = sys.argv[1], sys.argv[2]
t = ET.parse(src); d = t.getroot()
def one(path):
    e = d.findall(path)
    if len(e) != 1: sys.exit(f"expected exactly one {path} in {src}, found {len(e)}")
    return e[0]
one("name").text = "$NAME"
d.remove(one("uuid"))                       # libvirt generates a new UUID
for tag in ("title", "description"):
    for e in d.findall(tag): d.remove(e)
for tag in ("memory", "currentMemory"):
    for e in d.findall(tag): e.set("unit", "KiB"); e.text = "$MEM_KIB"
v = one("vcpu"); v.text = "$VCPUS"; v.attrib.pop("current", None)
for e in d.findall("cputune") + d.findall("numatune"): d.remove(e)

disks = d.findall("devices/disk")
repl = {"$IMG_DIR/$SRC.qcow2": "$DISK", "$SRC_SEED": "$SEED"}
seen = set()
for dk in disks:
    s = dk.find("source")
    f = s.get("file") if s is not None else None
    if f not in repl: sys.exit(f"unexpected disk source {f!r} in {src}")
    s.set("file", repl[f]); seen.add(f)
    for e in dk.findall("backingStore"): dk.remove(e)
if seen != set(repl): sys.exit(f"did not find both disks {list(repl)} in {src}")

ifs = d.findall("devices/interface")
if len(ifs) != 1: sys.exit(f"expected one interface, found {len(ifs)}")
i = ifs[0]
if i.get("type") != "bridge" or i.find("source").get("bridge") != "$BRIDGE" \
   or i.find("virtualport") is None or i.find("virtualport").get("type") != "openvswitch":
    sys.exit("tenant3 interface is not type=bridge/$BRIDGE/virtualport openvswitch; not guessing")
i.find("mac").set("address", "$MAC")
vp = i.find("virtualport")
for e in vp.findall("parameters"): vp.remove(e)    # drop interfaceid -> new one generated
for tag in ("target", "alias"):
    for e in i.findall(tag): i.remove(e)            # vnetN assigned automatically

nv = d.find("os/nvram")
if nv is not None:
    if nv.text and "$SRC" in nv.text: nv.text = "$NVRAM"   # new VARS file, created from template on start
    elif nv.text: sys.exit(f"unexpected nvram path {nv.text!r}")
for e in d.findall("seclabel"):
    if e.find("label") is not None or e.find("imagelabel") is not None: d.remove(e)
t.write(dst, encoding="unicode")
PY
for bad in "$SRC" "$SRC_UUID" "52:54:00:10:00:13" "seed3"; do
    if grep -qi -- "$bad" "$WORK/$NAME.xml"; then
        grep -ni -- "$bad" "$WORK/$NAME.xml" >&2
        die "new XML still contains '$bad'"
    fi
done
echo "--- diff tenant3 -> tenant5 XML"
diff "$WORK/$SRC.xml" "$WORK/$NAME.xml" || true
cp "$WORK/$NAME.xml" "$TB/$NAME.xml.defined" && chown "$OWNER:" "$TB/$NAME.xml.defined"

step "5) Define and start $NAME"
run virsh define "$WORK/$NAME.xml"
run virsh start "$NAME"
run virsh list --all

step "6) Result"
run virsh domiflist "$NAME"
DEV="$(virsh domiflist "$NAME" | awk -v m="$MAC" 'tolower($5)==m {print $1}')"
[[ -n "$DEV" && "$DEV" != "-" ]] || die "could not find host interface for $NAME"
echo "host interface: $DEV"
if [[ "$DEV" == "$EXPECT_DEV" ]]; then echo "OK: host interface is $EXPECT_DEV as expected"
else echo "NOTE: host interface is $DEV, not $EXPECT_DEV (harmless: exp38.py looks it up with virsh domiflist)"; fi
echo "OVS bridge of $DEV: $(ovs-vsctl port-to-br "$DEV")"
run ovs-vsctl --columns=name,ofport,external_ids list Interface "$DEV"
run ovs-vsctl list-ports "$BRIDGE"
ip -d link show "$DEV" | grep -q 'prog/xdp' && echo "NOTE: $DEV has an XDP program" || echo "$DEV: no XDP program (none was attached)"

echo; echo "Waiting up to 120 s for $IP to answer ping..."
ok=0
for ((s = 0; s < 120; s += 2)); do
    if ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
done
if ((ok)); then
    echo "OK: $IP answers ping after ~${s}s"; ping -c 3 "$IP"
else
    echo "FAILED: $IP did not answer within 120 s. Check: sudo virsh console $NAME (exit Ctrl+])" >&2
    exit 1
fi
cat <<EOF

Done. First boot also installs iperf3 and stress-ng from the internet (1-2 min). Then:
  ssh alpine@$IP 'hostname; ip -4 addr show eth0; cat /etc/resolv.conf; apk info | grep -E "iperf3|stress-ng"'
EOF
