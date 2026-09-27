#!/usr/bin/env bash
set -euo pipefail

# Test that the qemu hook installs the shared bridge drop rule once, however
# many VMs start. Run from anywhere:
#   test/test-bridge-drop-rule.sh
#
# The real hook runs with real nft inside `unshare -rn`, so it needs no VM and
# no sudo and never touches the host's ruleset. It is copied with /etc/migrant
# and /run/migrant pointed at a scratch directory, which the namespace can
# write. Each `prepare` is what libvirt runs as a VM starts.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$(cd "$SCRIPT_DIR/.." && pwd)/setup/qemu-hook"
STARTS=3

PASS=0
FAIL=0
pass() { echo "[PASS] $1"; (( PASS++ )) || true; }
fail() { echo "[FAIL] $1"; (( FAIL++ )) || true; }

command -v nft >/dev/null || { echo "[SKIP] nft not installed"; exit 0; }
unshare -rn true 2>/dev/null \
  || { echo "[SKIP] unprivileged user namespaces unavailable"; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

sed -e "s|/etc/migrant|$WORK/etc|g" -e "s|/run/migrant|$WORK/run|g" \
  "$HOOK" > "$WORK/qemu-hook"
chmod +x "$WORK/qemu-hook"

# Two VMs, so the starts also cover a second one joining the shared table.
for vm in drop-rule-a drop-rule-b; do
  mkdir -p "$WORK/etc/$vm"
  touch "$WORK/etc/$vm/network-isolation"
done

cat > "$WORK/in-ns.sh" <<'NS'
#!/usr/bin/env bash
set -euo pipefail
work="$1" starts="$2"
xml() {
  printf '%s\n' "<domain><description>managed-by=migrant</description>" \
    "<interface type='network'><mac address='$1'/></interface></domain>"
}
for (( i = 0; i < starts; i++ )); do
  xml 52:54:00:00:00:0a | "$work/qemu-hook" drop-rule-a prepare
  xml 52:54:00:00:00:0b | "$work/qemu-hook" drop-rule-b prepare
done
nft list chain bridge migrant prerouting | grep -c 'ether saddr @blocked_macs drop' || true
nft list set bridge migrant blocked_macs | grep -c '52:54:00:00:00:0[ab]' || true
NS
chmod +x "$WORK/in-ns.sh"

echo "=== Bridge drop rule test ==="
mapfile -t counts < <(unshare -rn "$WORK/in-ns.sh" "$WORK" "$STARTS")

if [[ "${counts[0]:-}" == 1 ]]; then
  pass "one drop rule after $(( STARTS * 2 )) VM starts"
else
  fail "expected 1 drop rule after $(( STARTS * 2 )) VM starts, found ${counts[0]:-none}"
  cat "$WORK/run/hook.log" 2>/dev/null || true
fi

# Both MACs blocked means the hook ran to the end, not out early.
if [[ "${counts[1]:-}" == 2 ]]; then
  pass "both VMs' MACs are in blocked_macs"
else
  fail "expected 2 blocked MACs, found ${counts[1]:-none}"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
(( FAIL == 0 ))
