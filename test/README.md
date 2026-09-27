# test/

Integration tests for migrant. Every test needs a working `migrant setup` and
KVM support on the host.

Each test script's header comment is its own spec: what it asserts, why, and the
tricks it uses to get there. This README covers how to run them and what they
need, not what they check — open the file for that.

## Shell tests

| Script                              | Covers                                  | Run from  | Needs                                  |
| ----------------------------------- | --------------------------------------- | --------- | -------------------------------------- |
| `test-hooks.sh`                     | lifecycle hook order and environment    | `test/vm` | boots a VM                             |
| `test-extra-args.sh`                | `.virt-install-extra-args` convention   | `test/vm` | boots a VM                             |
| `test-managed-config.sh`            | managed config, HOST_ACCESS rules       | `test/vm` | boots a VM, sudo                       |
| `test-multi-nic.sh`                 | per-tap rules on a two-NIC VM           | `test/vm` | boots a VM, sudo                       |
| `test-forward-port.sh`              | `forward-port` mappings                 | `test/vm` | boots a VM, sudo                       |
| `test-shared-folder.sh`             | shared folder isolation and sizing      | `test/vm` | boots a VM, sudo                       |
| `test-wireguard.sh`                 | WireGuard mode end to end               | `test/vm` | boots a VM, sudo, wireguard-tools, DNS |
| `test-resources.sh`                 | `RAM_MB`/`VCPUS` drift and validation   | anywhere  | libvirt                                |
| `test-shared-folder-drift.sh`       | `SHARED_FOLDERS` path drift             | anywhere  | libvirt                                |
| `test-snapshot.sh`                  | snapshot, reset, archive, restore       | anywhere  | libvirt                                |
| `test-managed-key-placeholder.sh`   | `__MIGRANT_PUBKEY__` in the seed ISO    | anywhere  | qemu-img, xorriso                      |
| `test-managed-key-ssh-opts.sh`      | managed key in ssh/provision opts       | anywhere  | —                                      |
| `test-ssh-key-path.sh`              | `SSH_KEY_PATH` precedence               | anywhere  | —                                      |
| `test-tunnel-connection-sharing.sh` | `tunnel` opts out of connection sharing | anywhere  | —                                      |
| `test-bridge-drop-rule.sh`          | one shared bridge drop rule per host    | anywhere  | nft, unprivileged user namespaces      |

**`test/vm`** scripts drive a real VM through its lifecycle and must be run from
the fixture directory:

```bash
cd test/vm && ../test-hooks.sh
```

Run `migrant pubkey` before the first one, to generate `~/.ssh/migrant` if it
does not exist yet — `test/vm/cloud-init.yml` references it via the
`__MIGRANT_PUBKEY__` placeholder, which `up` fills in automatically.

Do **not** run them from `examples/`. Every example sets `AUTOCONNECT`, which
leaves `migrant up` sitting in an interactive session — each script calls `up`
and then keeps going, so the run hangs. The examples also provision a full
toolchain over Ansible, which adds minutes to a test that only needs SSH.

**anywhere** scripts never boot a guest — they define a domain straight from
XML, or shadow `virsh`/`ssh`/`virt-install` on `PATH` — so they need no VM
directory, base image, or sudo:

```bash
test/test-resources.sh
```

### `vm/` — the fixture the first group runs from

A bare VM (`test-vm`): no `AUTOCONNECT`, no shared folder, one NIC, and no
`playbook.yml`, so Ansible never runs. The scripts probe with bash's `/dev/tcp`
rather than `netcheck.py`, so nothing needs installing in the guest.

Scripts that need a different shape — extra NICs, a shared folder, `HOST_ACCESS`
entries — back this Migrantfile up and write their own over it, restoring it on
exit. Keep the fixture minimal so they have a predictable base.

`vm/cloud-init.yml` masks the NTP units in `bootcmd` on purpose; the comment
there explains why removing it breaks `test-wireguard.sh`.

## VM test configs

Self-contained VM directories that verify HOST_ACCESS and network isolation
end-to-end. Each runs `netcheck.py` inside the VM to confirm connectivity
matches the Migrantfile.

```bash
cd test/<config>
../../migrant up      # creates VM, runs hooks, verifies via netcheck
../../migrant halt    # clean shutdown
../../migrant destroy # remove VM when done
```

| Config                 | What it tests                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `tcp-host-port/`       | `allow-host-port tcp/9999` to a `0.0.0.0` listener — and that the port is mapped from the gateway only, not hijacked from every address |
| `udp-host-port/`       | `allow-host-port udp/9999` — datagram to a host listener                                                                                |
| `localhost-host-port/` | `allow-host-port tcp/9998` to a **`127.0.0.1`** listener — the DNAT leg                                                                 |
| `lan-host/`            | `allow-lan-host` — the host's default router, auto-detected                                                                             |
| `multi-rule/`          | `allow-host-port tcp/9999` and `allow-lan-host` in one config                                                                           |
| `isolation-only/`      | default isolation, no HOST_ACCESS — the VM cannot reach the host                                                                        |
| `no-isolation/`        | `NETWORK_ISOLATION=false` — the VM reaches the host freely                                                                              |
| `ipv6-nat/`            | `NETWORK_IPV6=nat` — NAT66 egress works, host stays unreachable over IPv6                                                               |

### How a config is put together

- **Hooks.** `pre-up` starts a host-side listener before the VM boots, `post-up`
  runs `netcheck.py` in the guest and checks the result, `pre-down` kills the
  listener. Configs with no host-side service (`lan-host/`, `isolation-only/`,
  `no-isolation/`) have only `post-up`.
- **Delivery.** `playbook.yml` copies `tools/netcheck.py` into the guest home
  directory. Migrant runs the playbook once SSH and cloud-init are ready, so
  `post-up` can assume `~/netcheck.py` is there.
- **Base image.** Every config uses a copy of `test/cloud-init.yml` (Arch Linux,
  python3, uv).
