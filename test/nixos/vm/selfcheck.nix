# Optional self-check: on boot, run the whole migrant flow (doctor -> agent-up ->
# isolation asserts), write PASS/FAIL to the host via the qemu-vm shared dir, and
# power off. Layered onto host.nix by the `test-vm-check` flake output so the
# end-to-end test is one reliable command instead of external serial driving.
{ pkgs, ... }:
let
  netcheck = ../../../tools/netcheck.py;
  # Runs as the `nixos` user (libvirt group + managed key). Self-contained PATH
  # plus the system profile for `migrant`.
  check = pkgs.writeShellScript "migrant-selfcheck-inner" ''
    export PATH=${
      pkgs.lib.makeBinPath [
        pkgs.coreutils
        pkgs.gnugrep
        pkgs.gnused
        pkgs.gawk
        pkgs.curl
        pkgs.util-linux
      ]
    }:/run/current-system/sw/bin
    set -uo pipefail
    fail() { echo "SELFCHECK-FAIL: $*" >&2; exit 1; }

    migrant setup 2>&1 | tee /tmp/doctor.log
    grep -q "prerequisites satisfied" /tmp/doctor.log || fail "doctor did not pass"

    cd "$HOME/agent-vm" || fail "no agent-vm dir"
    sed -i '/^AUTOCONNECT=/d' Migrantfile
    img=/mnt/host-images/Arch-Linux-x86_64-cloudimg.qcow2
    if [ ! -f "$img" ]; then
      curl -fL --retry 3 -o "$img" \
        https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2 \
        || fail "base image fetch failed"
    fi
    key=$(migrant pubkey)
    awk -v k="$key" '{ if ($0 ~ /SSH_PLACEHOLDER/) print "      - " k; else print }' \
      cloud-init.yml.in > cloud-init.yml
    migrant up || true   # may exit non-zero on the 60s SSH timeout under nesting; we poll below

    # cloud-init is slow under double-nesting — poll up to ~10 min for key auth.
    ready=0
    for _ in $(seq 1 120); do
      migrant ssh -- true </dev/null >/dev/null 2>&1 && { ready=1; break; }
      sleep 5
    done
    [ "$ready" = 1 ] || fail "agent never became SSH-key-ready"

    # Verify isolation with the canonical netcheck.py — the same tool the test/
    # VM configs use — instead of ad-hoc probes. Default isolation (egress
    # allowed, host + LAN blocked, no HOST_ACCESS) makes a plain run exit 0.
    migrant ssh -- 'cat > netcheck.py' < ${netcheck} || fail "could not stage netcheck.py"
    migrant ssh -- 'uv run ~/netcheck.py --no-interactive' </dev/null 2>&1 || fail "netcheck reported an isolation failure"
    # netcheck covers the network; confirm the shared folder mounted too.
    migrant ssh -- 'mountpoint -q "$HOME/workspace"' </dev/null || fail "shared folder not mounted in agent"
    echo "SELFCHECK-OK"
  '';
in
{
  systemd.services.migrant-selfcheck = {
    description = "migrant end-to-end self-check";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      Type = "oneshot";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    path = with pkgs; [
      systemd
      util-linux
      coreutils
    ];
    script = ''
      mkdir -p /tmp/shared
      systemctl start libvirtd.service || true
      if runuser -l nixos -c ${check}; then
        echo "MIGRANT-SELFCHECK: PASS"
        echo PASS > /tmp/shared/result
      else
        echo "MIGRANT-SELFCHECK: FAIL"
        echo FAIL > /tmp/shared/result
      fi
      sync
      systemctl poweroff
    '';
  };
}
