#!/usr/bin/env bash
set -euo pipefail
export LIBVIRT_DEFAULT_URI="qemu:///system"

# Integration test for the .virt-install-extra-args file convention.
# Run from a VM directory that has a working Migrantfile + cloud-init.yml:
#   cd debian && ../test/test-extra-args.sh
#
# Verifies that a pre-up hook can contribute args to virt-install by writing
# to $VM_DIR/.virt-install-extra-args, and that the file is consumed (deleted)
# after being read. Exercises the convention by attaching a second read-only
# virtio-blk disk through the hook and checking the domain XML.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MIGRANT="$(cd "$SCRIPT_DIR/.." && pwd)/migrant"

if [[ ! -f Migrantfile ]]; then
  echo "[FAIL] No Migrantfile in $(pwd). Run from a VM directory." >&2
  exit 1
fi

# shellcheck source=/dev/null
source Migrantfile

HOOKS_DIR="./hooks"
TEST_HOOK="$HOOKS_DIR/pre-up"
TEST_HOOK_BACKUP=""
ARGS_FILE="./.virt-install-extra-args"
TEST_DISK="/tmp/migrant-extra-args-test-$$.img"
PASS=0
FAIL=0

pass() { echo "[PASS] $1"; (( PASS++ )) || true; }
fail() { echo "[FAIL] $1"; (( FAIL++ )) || true; }

cleanup() {
  # Restore any pre-existing pre-up hook the test displaced; otherwise
  # remove the one the test installed. Never rm -rf $HOOKS_DIR — it may
  # contain unrelated hooks that predate this test.
  if [[ -n "$TEST_HOOK_BACKUP" && -f "$TEST_HOOK_BACKUP" ]]; then
    mv "$TEST_HOOK_BACKUP" "$TEST_HOOK"
  else
    rm -f "$TEST_HOOK"
  fi
  rm -f "$ARGS_FILE"
  rm -f "$TEST_DISK"
  virsh dominfo "$VM_NAME" &>/dev/null && "$MIGRANT" destroy 2>/dev/null || true
}
trap cleanup EXIT

# --- setup ---

echo "=== .virt-install-extra-args convention test ==="
echo "VM: $VM_NAME"
echo ""

# Ensure no VM exists from a previous run.
if virsh dominfo "$VM_NAME" &>/dev/null; then
  echo "Cleaning up leftover VM '$VM_NAME'..."
  "$MIGRANT" destroy 2>/dev/null || true
fi

# Create a small extra disk image to attach via the convention.
qemu-img create -f qcow2 "$TEST_DISK" 16M >/dev/null

# Install a pre-up hook that writes the extra-args file.
# Preserve any existing hook so cleanup can restore it.
mkdir -p "$HOOKS_DIR"
if [[ -f "$TEST_HOOK" ]]; then
  TEST_HOOK_BACKUP="$TEST_HOOK.test-extra-args.bak"
  mv "$TEST_HOOK" "$TEST_HOOK_BACKUP"
fi
cat > "$TEST_HOOK" <<HOOKEOF
#!/usr/bin/env bash
set -euo pipefail
cat > "\$MIGRANT_VM_DIR/.virt-install-extra-args" <<ARGS
--disk
path=$TEST_DISK,bus=virtio,readonly=on
ARGS
HOOKEOF
chmod +x "$TEST_HOOK"

# --- test 1: first create consumes the file and attaches the disk ---

echo "--- test: first create attaches the extra disk ---"
"$MIGRANT" up

if [[ ! -f "$ARGS_FILE" ]]; then
  pass ".virt-install-extra-args consumed (deleted) after pre-up"
else
  fail ".virt-install-extra-args still present after up"
fi

if virsh dumpxml "$VM_NAME" | grep -q "$TEST_DISK"; then
  pass "extra disk present in domain XML"
else
  fail "extra disk not found in domain XML"
  virsh dumpxml "$VM_NAME" | grep -A1 disk
fi

# --- test 2: missing file is a no-op (regression) ---

echo "--- test: absent file does not disturb virt-install ---"
"$MIGRANT" destroy
rm -f "$TEST_HOOK"
"$MIGRANT" up
pass "up works with no .virt-install-extra-args (regression against file-absent path)"

# --- summary ---

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if (( FAIL > 0 )); then
  exit 1
fi
