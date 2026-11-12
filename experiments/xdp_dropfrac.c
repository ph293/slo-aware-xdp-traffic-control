#include <linux/bpf.h>
/* build (no sudo): clang -O2 -g -target bpf -mcpu=v3 -I/usr/include/aarch64-linux-gnu -c xdp_dropfrac.c -o xdp_dropfrac.o
   (-mcpu=v3 for the atomic fetch-add whose return value is used) */
#include <bpf/bpf_helpers.h>
/* exp42: deterministic drop fraction (the actuator of Bisevac et al.'s LQR controller), generic-mode XDP on one
   flooder vnet. Same cnt map, layout and accounting as xdp_limit.c; cfg holds drop_bp instead of a rate.
   drop iff (total * drop_bp) % 10000 < drop_bp, total = this vnet's packet counter before this packet (every
   packet counted, dropped or not); drop_bp == 0 means no drop. No management-traffic exemption (none enabled). */

struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1);
         __type(key, __u32); __type(value, __u64); } cfg SEC(".maps");  /* drop_bp, basis points 0..9500, 0 = no drop */
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1);
         __type(key, __u32); __type(value, __u64); } seq SEC(".maps");  /* per-tenant packet counter "total" */
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 4);
         __type(key, __u32); __type(value, __u64); } cnt SEC(".maps");  /* 0 pass pkts, 1 drop pkts, 2 pass bytes, 3 drop bytes */

static __always_inline void add(__u32 idx, __u64 v) {
  __u64 *p = bpf_map_lookup_elem(&cnt, &idx);
  if (p) __sync_fetch_and_add(p, v);
}

SEC("xdp")
int xdp_dropfrac(struct xdp_md *ctx) {
  __u32 k = 0;
  __u64 *bp = bpf_map_lookup_elem(&cfg, &k);
  __u64 *s = bpf_map_lookup_elem(&seq, &k);
  __u64 len = (__u64)(ctx->data_end - ctx->data);
  if (!bp || !s) { add(0, 1); add(2, len); return XDP_PASS; }
  __u64 total = __sync_fetch_and_add(s, 1);
  __u64 d = *bp;
  if (d != 0 && (total * d) % 10000 < d) { add(1, 1); add(3, len); return XDP_DROP; }
  add(0, 1); add(2, len);
  return XDP_PASS;
}
char _license[] SEC("license") = "GPL";
