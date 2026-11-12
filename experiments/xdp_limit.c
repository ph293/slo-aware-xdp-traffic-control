#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1);
         __type(key, __u32); __type(value, __u64); } cfg SEC(".maps");  /* bytes/sec, 0 = unlimited */
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1);
         __type(key, __u32); __type(value, __u64); } st SEC(".maps");   /* virtual clock, ns */
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 4);
         __type(key, __u32); __type(value, __u64); } cnt SEC(".maps");  /* 0 pass pkts, 1 drop pkts, 2 pass bytes, 3 drop bytes */

#define BURST_NS 5000000ULL

static __always_inline void add(__u32 idx, __u64 v) {
  __u64 *p = bpf_map_lookup_elem(&cnt, &idx);
  if (p) __sync_fetch_and_add(p, v);
}

SEC("xdp")
int xdp_limit(struct xdp_md *ctx) {
  __u32 k = 0;
  __u64 *rate = bpf_map_lookup_elem(&cfg, &k);
  __u64 *t = bpf_map_lookup_elem(&st, &k);
  __u64 len = (__u64)(ctx->data_end - ctx->data);
  if (!rate || !t || *rate == 0) { add(0, 1); add(2, len); return XDP_PASS; }
  __u64 now = bpf_ktime_get_ns();
  __u64 cur = *t;
  if (cur < now) cur = now;
  if (cur - now > BURST_NS) { add(1, 1); add(3, len); return XDP_DROP; }
  *t = cur + len * 1000000000ULL / *rate;
  add(0, 1); add(2, len);
  return XDP_PASS;
}
char _license[] SEC("license") = "GPL";
