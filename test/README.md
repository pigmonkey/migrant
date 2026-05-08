# test/

Integration tests for migrant. All tests require a working
`migrant setup` and KVM support on the host.

---

## Shell test scripts

Run from any VM directory (e.g. `cd debian && ../test/test-hooks.sh`):

- **test-hooks.sh** — lifecycle hook execution, ordering, and environment
  variables
- **test-managed-config.sh** — managed config files, HOST_ACCESS
  validation, iptables rule creation and cleanup
- **test-extra-args.sh** — `$VM_DIR/.virt-install-extra-args` file convention:
  pre-up hook contributes args to virt-install on first create, file is
  consumed on read, absent file is a no-op

---

## VM test configs

Self-contained VM directories that verify HOST_ACCESS and network
isolation end-to-end. Each runs netcheck.py inside the VM to confirm
connectivity matches the Migrantfile configuration.

```bash
cd test/<config>
../../migrant up      # creates VM, runs hooks, verifies via netcheck
../../migrant halt    # clean shutdown
../../migrant destroy # remove VM when done
```

| Config | What it tests |
| ------ | ------------- |
| `tcp-host-port/` | `allow-host-port tcp/9999` — TCP listener on host, VM connects through isolation |
| `udp-host-port/` | `allow-host-port udp/9999` — UDP listener on host, VM sends datagram through isolation |
| `lan-host/` | `allow-lan-host` — VM reaches the host's default router (auto-detected) |
| `multi-rule/` | Combined `allow-host-port tcp/9999` + `allow-lan-host` in a single config |
| `isolation-only/` | Default isolation with no HOST_ACCESS — verifies the VM cannot reach the host |
| `no-isolation/` | `NETWORK_ISOLATION=false` — verifies the VM can reach the host freely |

### Hook pattern

Configs that start a host-side service use this hook layout:

| Hook | Purpose |
| ---- | ------- |
| `pre-up` | Start a listener on the host before the VM boots |
| `post-up` | Run netcheck.py inside the VM and verify the result |
| `pre-down` | Kill the listener before the VM stops |

Configs without a host-side service (`lan-host/`, `isolation-only/`,
`no-isolation/`) only have a `post-up` hook.

### File delivery via Ansible

Each config's `playbook.yml` copies `tools/netcheck.py` into the VM
home directory. Migrant.sh runs the playbook automatically once SSH
and cloud-init are ready, so the post-up hook can assume `~/netcheck.py`
exists and just runs it.

### Shared cloud-init

All configs use a copy of `test/cloud-init.yml` (Debian, python3, uv).
