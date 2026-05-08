#!/usr/bin/env bash
set -euo pipefail
export LIBVIRT_DEFAULT_URI="qemu:///system"

# Integration test for lifecycle hooks.
# Run from a VM directory that has a working Migrantfile + cloud-init.yml:
#   cd debian && ../test/test-hooks.sh
#
# Prerequisites:
#   - migrant setup has been run
#   - The base image is cached (or will be downloaded)
#   - No VM with this name currently exists (the test creates and destroys one)
#
# The test installs temporary hook scripts, runs the VM through its lifecycle,
# and verifies that hooks fired at the right times with the right environment.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MIGRANT="$(cd "$SCRIPT_DIR/.." && pwd)/migrant"

if [[ ! -f Migrantfile ]]; then
  echo "[FAIL] No Migrantfile in $(pwd). Run from a VM directory." >&2
  exit 1
fi

# shellcheck source=/dev/null
source Migrantfile

LOG="/tmp/migrant-hook-test-$$"
HOOKS_DIR="./hooks"
PASS=0
FAIL=0

pass() { echo "[PASS] $1"; (( PASS++ )) || true; }
fail() { echo "[FAIL] $1"; (( FAIL++ )) || true; }

cleanup() {
  rm -rf "$HOOKS_DIR"
  rm -f "$LOG".*
  # Leave the VM alone — the test destroys it as part of the lifecycle.
}
trap cleanup EXIT

# --- setup test hooks ---

mkdir -p "$HOOKS_DIR"
for hook in pre-up post-up pre-down post-down; do
  cat > "$HOOKS_DIR/$hook" <<HOOKEOF
#!/usr/bin/env bash
echo "\$MIGRANT_HOOK \$MIGRANT_TRIGGER \$MIGRANT_VM_NAME \$MIGRANT_VM_IP" >> "$LOG.\$MIGRANT_HOOK"
HOOKEOF
  chmod +x "$HOOKS_DIR/$hook"
done

echo "=== Lifecycle hooks test ==="
echo "VM: $VM_NAME"
echo "Log prefix: $LOG"
echo ""

# Ensure no VM exists from a previous run.
if virsh dominfo "$VM_NAME" &>/dev/null; then
  echo "Cleaning up leftover VM '$VM_NAME'..."
  "$MIGRANT" destroy 2>/dev/null || true
fi

# --- test 1: pre-up abort ---

echo "--- test: pre-up abort ---"
cat > "$HOOKS_DIR/pre-up" <<'HOOKEOF'
#!/usr/bin/env bash
exit 1
HOOKEOF
chmod +x "$HOOKS_DIR/pre-up"

abort_output="$LOG.abort"
"$MIGRANT" up >"$abort_output" 2>&1 || true
if grep -q "pre-up hook failed" "$abort_output"; then
  pass "pre-up abort prevents VM start"
else
  fail "pre-up abort did not prevent VM start"
  cat "$abort_output"
  # Clean up if VM was created despite the abort
  "$MIGRANT" destroy 2>/dev/null || true
fi
rm -f "$abort_output"

# Restore the logging hook
cat > "$HOOKS_DIR/pre-up" <<HOOKEOF
#!/usr/bin/env bash
echo "\$MIGRANT_HOOK \$MIGRANT_TRIGGER \$MIGRANT_VM_NAME \$MIGRANT_VM_IP" >> "$LOG.\$MIGRANT_HOOK"
HOOKEOF
chmod +x "$HOOKS_DIR/pre-up"

# --- test 2: up (create) fires pre-up and post-up ---

echo "--- test: up (create) ---"
"$MIGRANT" up

if [[ -f "$LOG.pre-up" ]]; then
  pass "pre-up hook fired on create"
else
  fail "pre-up hook did not fire on create"
fi

if [[ -f "$LOG.post-up" ]]; then
  pass "post-up hook fired on create"
else
  fail "post-up hook did not fire on create"
fi

# Check environment variables
if grep -q "pre-up up $VM_NAME" "$LOG.pre-up" 2>/dev/null; then
  pass "pre-up received correct MIGRANT_HOOK, MIGRANT_TRIGGER, MIGRANT_VM_NAME"
else
  fail "pre-up environment incorrect (expected: pre-up up $VM_NAME)"
  cat "$LOG.pre-up" 2>/dev/null || true
fi

if grep -q "post-up up $VM_NAME" "$LOG.post-up" 2>/dev/null; then
  pass "post-up received correct environment"
else
  fail "post-up environment incorrect"
  cat "$LOG.post-up" 2>/dev/null || true
fi

# Check that post-up has the VM IP (non-empty fourth field)
if awk '{print $4}' "$LOG.post-up" 2>/dev/null | grep -qE '^[0-9]+\.'; then
  pass "post-up received MIGRANT_VM_IP"
else
  fail "post-up MIGRANT_VM_IP is empty or missing"
fi

rm -f "$LOG".*

# --- test 3: halt fires pre-down and post-down ---

echo "--- test: halt ---"
"$MIGRANT" halt

if grep -q "pre-down halt $VM_NAME" "$LOG.pre-down" 2>/dev/null; then
  pass "pre-down fired on halt with correct environment"
else
  fail "pre-down did not fire on halt or environment incorrect"
  cat "$LOG.pre-down" 2>/dev/null || true
fi

if grep -q "post-down halt $VM_NAME" "$LOG.post-down" 2>/dev/null; then
  pass "post-down fired on halt with correct environment"
else
  fail "post-down did not fire on halt or environment incorrect"
fi

# Check that pre-down has the VM IP
if awk '{print $4}' "$LOG.pre-down" 2>/dev/null | grep -qE '^[0-9]+\.'; then
  pass "pre-down received MIGRANT_VM_IP on halt"
else
  fail "pre-down MIGRANT_VM_IP is empty on halt"
fi

rm -f "$LOG".*

# --- test 4: up (start existing) fires hooks ---

echo "--- test: up (start existing) ---"
"$MIGRANT" up

if grep -q "pre-up up $VM_NAME" "$LOG.pre-up" 2>/dev/null; then
  pass "pre-up fired on start-existing"
else
  fail "pre-up did not fire on start-existing"
fi

if [[ -f "$LOG.post-up" ]]; then
  pass "post-up fired on start-existing"
else
  fail "post-up did not fire on start-existing"
fi

rm -f "$LOG".*

# --- test 5: snapshot fires pre-down and post-down ---

echo "--- test: snapshot ---"
"$MIGRANT" snapshot

if grep -q "pre-down snapshot $VM_NAME" "$LOG.pre-down" 2>/dev/null; then
  pass "pre-down fired on snapshot with MIGRANT_TRIGGER=snapshot"
else
  fail "pre-down did not fire on snapshot"
  cat "$LOG.pre-down" 2>/dev/null || true
fi

if grep -q "post-down snapshot $VM_NAME" "$LOG.post-down" 2>/dev/null; then
  pass "post-down fired on snapshot"
else
  fail "post-down did not fire on snapshot"
fi

rm -f "$LOG".*

# --- test 6: reset fires down hooks then up hooks ---

echo "--- test: reset ---"
"$MIGRANT" reset

if grep -q "pre-down reset $VM_NAME" "$LOG.pre-down" 2>/dev/null; then
  pass "pre-down fired on reset"
else
  fail "pre-down did not fire on reset"
fi

if grep -q "post-down reset $VM_NAME" "$LOG.post-down" 2>/dev/null; then
  pass "post-down fired on reset"
else
  fail "post-down did not fire on reset"
fi

# reset calls cmd_up internally — _MIGRANT_TRIGGER is already "reset" when
# cmd_up runs, so all four hooks (pre-down, post-down, pre-up, post-up) receive
# MIGRANT_TRIGGER=reset.
if [[ -f "$LOG.pre-up" ]]; then
  pass "pre-up fired during reset rebuild"
else
  fail "pre-up did not fire during reset rebuild"
fi

if [[ -f "$LOG.post-up" ]]; then
  pass "post-up fired during reset rebuild"
else
  fail "post-up did not fire during reset rebuild"
fi

rm -f "$LOG".*

# --- test 7: destroy fires down hooks (non-aborting) ---

echo "--- test: destroy ---"

# Install a pre-down that tries to abort — should be ignored by destroy
cat > "$HOOKS_DIR/pre-down" <<HOOKEOF
#!/usr/bin/env bash
echo "\$MIGRANT_HOOK \$MIGRANT_TRIGGER \$MIGRANT_VM_NAME \$MIGRANT_VM_IP" >> "$LOG.\$MIGRANT_HOOK"
exit 1
HOOKEOF
chmod +x "$HOOKS_DIR/pre-down"

"$MIGRANT" destroy

if grep -q "pre-down destroy $VM_NAME" "$LOG.pre-down" 2>/dev/null; then
  pass "pre-down fired on destroy"
else
  fail "pre-down did not fire on destroy"
fi

if grep -q "post-down destroy $VM_NAME" "$LOG.post-down" 2>/dev/null; then
  pass "post-down fired on destroy (pre-down failure did not block)"
else
  fail "post-down did not fire on destroy"
fi

rm -f "$LOG".*

# --- test 8: no hooks directory = no errors ---

echo "--- test: no hooks ---"
rm -rf "$HOOKS_DIR"
# This should work without errors (snapshot was created earlier)
"$MIGRANT" up
"$MIGRANT" halt
"$MIGRANT" destroy
pass "lifecycle completes without hooks directory"

# --- summary ---

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if (( FAIL > 0 )); then
  exit 1
fi
