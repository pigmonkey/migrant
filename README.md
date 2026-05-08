# migrant

A lightweight, Vagrant-like VM management tool for Linux, built on
**libvirt + QEMU/KVM**. Define a VM in a `Migrantfile` file, drop a
`cloud-init.yml` alongside it, and use a single script to create, start,
stop, and destroy virtual machines — each with its own kernel, isolated
from the host.

Designed as a replacement for Vagrant when running ephemeral agent VMs
(e.g. [Claude Code](https://docs.anthropic.com/en/docs/claude-code)) on
Linux hosts.

---

## LLM Warning

The script itself (and all of the README other than this section) was [written
by an isolated Claude Code
agent](https://en.wikipedia.org/wiki/Eating_your_own_dog_food), but I would not
call it, as The Kids say, "vibe-coded". Design decisions were made by me (a
[real human being](https://www.youtube.com/watch?v=-DSVDcw6iW8)). I am
hyper-critical of Claude's shell scripting abilities. I read and question
every line, often redirecting it down another path.

---

## Why not Vagrant?

Vagrant is a solid tool, but has some drawbacks for this use case:

|                         | Vagrant + VirtualBox             | migrant + KVM                            |
| ----------------------- | -------------------------------- | ---------------------------------------- |
| Hypervisor              | VirtualBox (userspace)           | KVM (Linux kernel native)                |
| Shared folders          | `vboxsf` via guest kernel module | `virtiofs` via host daemon               |
| Default user privileges | Passwordless sudo (vagrant user) | Configurable via cloud-init              |
| Rebuild speed           | Slow (full image copy)           | Fast (qcow2 backing file, copy-on-write) |
| Dependency footprint    | Vagrant + VirtualBox             | libvirt + QEMU (standard Linux stack)    |
| Config format           | Ruby (Vagrantfile)               | Bash (Migrantfile) + YAML (cloud-init)   |

The most important difference is isolation. VirtualBox shared folders
require a kernel module running inside the guest (`vboxsf`), which
increases the attack surface between the guest and host. `virtiofs`
instead uses a daemon on the host side; the guest interacts with it over
a virtio channel without any special kernel module. Combined with KVM's
smaller hypervisor attack surface compared to VirtualBox, this makes
`migrant` a better fit for running untrusted or autonomous workloads.

---

## How it works

Each project directory contains these files:

- **`Migrantfile`** — a sourced bash file declaring VM name, resources,
  image, and shared folders
- **`cloud-init.yml`** — a standard
  [cloud-init](https://cloudinit.readthedocs.io/) user-data file that
  handles first-boot system setup: creating users, configuring SSH keys,
  and mounting shared folders
- **`playbook.yml`** (optional) — an [Ansible](https://docs.ansible.com/)
  playbook for ongoing configuration management: installing packages,
  deploying dotfiles, and anything that may change over the VM's lifetime

The `migrant` script lives in your `PATH` and reads these files from
the current directory by default, just like `vagrant` reads a `Vagrantfile`.
Alternatively, set the `MIGRANT_DIR` environment variable to point at the
project directory and run `migrant` from anywhere (see [MIGRANT_DIR](#migrant_dir)).

On first `migrant up`, the script:

1. Downloads the base cloud image (once, cached in `/var/lib/libvirt/images/`)
2. Creates a qcow2 disk using the base image as a backing file
   (copy-on-write — fast, no full copy)
3. Packages your `cloud-init.yml` into a seed ISO
4. Calls `virt-install` to define and start the VM
5. cloud-init runs inside the VM on first boot to create users, configure
   SSH keys, and mount shared folders
6. If `playbook.yml` is present, waits for SSH to become available, waits
   for cloud-init to finish, then runs `ansible-playbook` to complete
   provisioning; `up` blocks until done and the VM is fully ready when it
   returns

On subsequent `migrant up` calls, the VM already exists so the script
starts it with `virsh start`, then waits for SSH if configured.

Destroying the VM with `migrant destroy` removes the libvirt domain
and deletes the VM's disk, seed ISO, and any snapshot, leaving the
cached base image intact so the next `migrant up` is fast.

---

## Installation (Arch Linux)

### Prerequisites: verify KVM support

`migrant` relies on KVM hardware acceleration. Without it, VMs are
created via software emulation and are impractically slow. Verify that
your CPU supports virtualization and that it is enabled in BIOS before
continuing:

```bash
lscpu | grep Virtualization
ls /dev/kvm
```

`lscpu` should show `VT-x` (Intel) or `AMD-V` (AMD). `/dev/kvm` should
exist. If either is missing, enter your BIOS/UEFI settings and enable
Intel VT-x / AMD-V (sometimes labelled "Virtualization Technology" or
"SVM Mode").

### 1. Install dependencies

```bash
sudo pacman -S qemu-base libvirt virt-install dnsmasq libisoburn
```

`dnsmasq` must be installed so libvirt can use its binary for guest
DHCP/DNS, but do not enable the dnsmasq systemd service — libvirt
manages its own dnsmasq process internally.

If you plan to use Ansible provisioning (`playbook.yml`), also install:

```bash
sudo pacman -S ansible
```

Ansible runs on the host and connects to the VM over SSH. An SSH key must
be configured in `cloud-init.yml` (see [Managed SSH key](#managed-ssh-key-recommended))
before running Ansible.

### 2. Install migrant

```bash
cp migrant ~/bin/migrant
chmod +x ~/bin/migrant
```

Make sure `~/bin` is in your `PATH`. Add this to your `~/.bashrc` or
`~/.zshrc` if needed:

```bash
export PATH="$PATH:$HOME/bin"
```

### 3. Run one-time host setup

```bash
migrant setup
```

This configures everything needed to use migrant: enables the libvirtd and
virtlogd sockets, adds your user to the `libvirt` group, detects the host
firewall backend (iptables or nftables) and updates `/etc/libvirt/network.conf`
to match, defines the `migrant` NAT network, creates the images directory with
group-writable permissions, installs three libvirt hooks (network isolation and
WireGuard tunnel management, shared folder loop image mount/unmount, and
`rp_filter` for the `linux-hardened` kernel), creates `/etc/migrant/` for
managed VM configs, and installs ZSH completions if `$ZSH_SITE_FUNCTIONS`
is set.

If your user was not already in the `libvirt` group, setup will add it and then
fail — the group change is not live in the current session. Log out and back in
(or run `newgrp libvirt`) and re-run `migrant setup` to complete the
remaining steps.

`setup` is idempotent — re-run it after upgrading migrant to update the hooks.

#### Firewall caveats

If you run an **nftables firewall** (`nftables.service` active with a
custom ruleset), be aware of two issues with standard Arch example
configurations:

- The Workstation and Server example configs both include a `forward`
  chain with `policy drop`. This drops all packets routed between
  interfaces, blocking VM traffic on `virbr-migrant`. Any nftables config
  must either omit the `forward` chain or add explicit accept rules
  for `virbr-migrant` traffic.

- Both example configs start with `flush ruleset`. Reloading
  `nftables.service` will wipe libvirt's rules until libvirt restarts.
  Avoid reloading nftables while VMs are running, or use the
  [atomic reload](https://wiki.archlinux.org/title/Nftables#Atomic_reloading)
  technique to prepend libvirt's rules to your config.

If you also run **Docker on the host**, Docker and libvirt both modify
firewall rules at startup. If they use the same backend, reloading
either service can disrupt the other's networking. The Arch nftables
wiki recommends running Docker in a separate network namespace to avoid
this conflict. See the
[Working with Docker](https://wiki.archlinux.org/title/Nftables#Working_with_Docker)
section for the drop-in configuration.

---

## Example: Claude Code agent VMs

The `arch/`, `ubuntu/`, and `debian/` subdirectories contain ready-to-use
examples for running [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
in an isolated VM on Arch Linux, Ubuntu, and Debian Trixie respectively.
They use both provisioning methods:

- **`cloud-init.yml`** handles system bootstrap: creating the `migrant` user,
  configuring SSH, and mounting the shared folder
- **`playbook.yml`** handles software setup: installing packages, claude-code,
  uv, and bash aliases

The `cloud-init.yml` also contains the equivalent cloud-init-only setup
commented out, as a reference for using either approach.

First, generate the managed SSH key and add it to `cloud-init.yml`
(required for Ansible provisioning):

```bash
cd ubuntu
migrant pubkey    # generates ~/.ssh/migrant if needed; prints the public key
```

Paste the output into `cloud-init.yml` under `ssh_authorized_keys`. The
comment must remain `migrant` so migrant recognises it. Then:

```bash
migrant up        # creates VM, runs cloud-init + Ansible; blocks until ready
migrant ssh
```

---

## Usage

Run commands from the project directory containing `Migrantfile`, or set
`MIGRANT_DIR` to run from anywhere (see [MIGRANT_DIR](#migrant_dir)).

```bash
# Setup
migrant setup              # One-time host setup: configures libvirt networking and installs firewall hooks

# Lifecycle
migrant up                 # Create the VM if it does not exist, or start it if stopped; runs Ansible provisioning (if playbook.yml exists) on first create; waits until the VM is fully ready; connects automatically if AUTOCONNECT is set in the Migrantfile
migrant halt               # Gracefully shut down the VM
migrant destroy            # Stop and permanently delete the VM, its disk, and any snapshots
migrant status             # Show the VM's current state and snapshot availability
migrant provision          # Run the Ansible playbook (playbook.yml) against the running VM
migrant snapshot           # Shut down the VM and save a snapshot of its disk; VM stays down afterward
migrant reset              # Destroy the VM and rebuild it from the last snapshot

# Shared folder
migrant mount              # Mount the shared folder loop image for host-side access; creates the image if it does not exist
migrant unmount            # Unmount the shared folder loop image

# Access
migrant ssh [-- cmd...]    # SSH into the VM as the configured user; optionally run a remote command (e.g. migrant ssh -- sudo cloud-init status)
migrant console            # Open a serial console session (exit with Ctrl+])
migrant ip                 # Print the VM's IP address
migrant pubkey             # Generate the managed SSH key if needed and print its public key
migrant tz [zone]          # Sync the host timezone to the VM, or set an explicit zone (e.g. America/New_York); defaults to the host timezone

# Diagnostics
migrant storage            # List IMAGES_DIR contents grouped by base images and VMs, with file sizes; works without a Migrantfile
migrant wg                 # Show live WireGuard interface status, including transfer stats and latest handshake; requires sudo
migrant dominfo            # Show detailed libvirt domain info for the VM
```

### Typical workflow

```bash
# First time
cd ~/my-agent-vm
migrant up          # creates VM, runs cloud-init + Ansible; blocks until ready
migrant ssh         # connect and do any manual one-time setup (e.g. auth)
migrant snapshot    # save this known-good state

# Day-to-day
migrant up       # start
migrant halt     # stop when done

# Restore to snapshot
migrant reset    # wipe and rebuild from snapshot; Ansible does not re-run
                    # (the snapshot already contains its output)

# Update provisioning after changing playbook.yml
migrant up
migrant provision   # re-run the Ansible playbook; VM stays running

# Start completely fresh
migrant destroy
migrant up
```

### MIGRANT_DIR

Set `MIGRANT_DIR` to the path of a project directory to run any command
without `cd`-ing into it first:

```bash
MIGRANT_DIR=~/migrant/ubuntu migrant up
MIGRANT_DIR=~/migrant/ubuntu migrant halt
```

The typical use is to define a shell alias:

```bash
alias mig-a="MIGRANT_DIR=$HOME/migrant/arch migrant"
alias mig-d="MIGRANT_DIR=$HOME/migrant/debian migrant"
alias mig-u="MIGRANT_DIR=$HOME/migrant/ubuntu migrant"
```

After which you can manage the VM from anywhere:

```bash
mig-u up
mig-u halt
mig-u ssh
```

Note: use `$HOME` rather than `~` when defining the alias, since `~` inside
quotes is not expanded by the shell and would be passed to the script
literally.

Shared folder paths in `Migrantfile` that do not begin with `/` are always
resolved relative to the `Migrantfile`'s directory, regardless of where
`migrant` is invoked from.

### Waiting for the VM to be ready

`migrant up` blocks until the VM obtains a DHCP lease (unless
`AUTOCONNECT=console` is set and no `playbook.yml` is present, in which
case it attaches the console immediately after the VM starts). If the VM
stops running while waiting (e.g. due to a crash or misconfiguration),
`up` exits with an error rather than waiting indefinitely.

If SSH is configured in `cloud-init.yml` (`ssh_authorized_keys` present),
`up` additionally waits until SSH is available before returning. This
applies both when starting a stopped VM and when creating one with
`playbook.yml`.

If `playbook.yml` is present, `up` goes further still: it waits for
cloud-init to finish and then runs Ansible, returning only when the VM
is fully provisioned. Setting `CLOUD_INIT_WAIT=false` in the
Migrantfile skips the cloud-init wait. This is useful for images where
provisioning is baked in rather than handled by cloud-init at boot.
Ansible still runs if `playbook.yml` is present.

Without `playbook.yml`, the IP and SSH waits are the only signals that
the VM is ready. On a first boot, packages may still be installing in
the background when `up` returns.

Setting `AUTOCONNECT` in the Migrantfile causes `up` to connect
automatically once the VM is ready, without needing a separate
`migrant ssh` or `migrant console` invocation:

```bash
AUTOCONNECT=ssh      # connect via SSH after up completes
AUTOCONNECT=console  # attach serial console immediately after the VM starts
```

`AUTOCONNECT=console` skips the IP and SSH waits and attaches as soon
as the VM starts, so the boot output is visible. If `playbook.yml` is
present, provisioning runs first and the console attaches afterward.

### Network lifecycle

`migrant up` starts the `migrant` libvirt network (`virbr-migrant`, 192.168.200.0/24) automatically
if it exists but is not currently active. `migrant setup` only creates
(defines) the network — starting it is left to `up` so the network is not
running unnecessarily when no VMs are in use.

`migrant halt` shuts down any libvirt networks listed in the `NETWORKS`
config that are no longer in use. If other running VMs are still attached to a
network, it is left running; otherwise it is stopped. This keeps the libvirt
bridge interfaces off the host when idle.

### Serial console vs SSH

`migrant console` opens a serial console via `virsh console`. This is
not SSH — it connects directly to the VM's serial port, like a physical
terminal. To exit the console, press `Ctrl+]`.

To log in via the console, the user defined in `cloud-init.yml` must
have a password set. cloud-init locks passwords by default for users
defined in the `users:` list. Add `lock_passwd: false` and either a
plaintext or hashed password to enable console login:

```yaml
users:
  - name: migrant
    lock_passwd: false
    plain_text_passwd: "yourpassword"
```

For production use, prefer a pre-hashed password (generated with
`openssl passwd -6`) so the plaintext never appears in the config file:

```yaml
users:
  - name: migrant
    lock_passwd: false
    passwd: "$6$..."   # openssl passwd -6 yourpassword
```

`migrant ssh` looks up the VM's IP address and SSHes in as the first
user defined in `cloud-init.yml`.

Host key verification is disabled (`StrictHostKeyChecking=no`,
`UserKnownHostsFile=/dev/null`) because these VMs are ephemeral —
rebuilding a VM generates a new host key at the same IP, which would
cause a standard SSH client to refuse the connection.

#### Managed SSH key (recommended)

migrant can manage a dedicated passphrase-less SSH key at
`~/.ssh/migrant`, shared across all VMs that use it. This is detected
automatically: if `cloud-init.yml` contains a key whose comment is
`migrant`, migrant uses `~/.ssh/migrant` exclusively for SSH
connections (`IdentitiesOnly=yes`).

First-time setup:

```bash
migrant pubkey    # generates ~/.ssh/migrant if needed; prints the public key
```

Paste the output into `cloud-init.yml` under `ssh_authorized_keys`:

```yaml
users:
  - name: migrant
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... migrant
```

Then create the VM:

```bash
migrant up
migrant ssh       # uses ~/.ssh/migrant automatically
```

migrant verifies at `up` time that the key in `cloud-init.yml` matches
`~/.ssh/migrant.pub` and errors early if not, since a mismatch would mean
the VM boots with a key the host cannot use. If `~/.ssh/migrant` is ever
lost, run `migrant pubkey` to regenerate it, update `cloud-init.yml`,
and rebuild with `migrant destroy && migrant up`.

#### Manual key management

Without a `migrant`-commented key, migrant expects you to have added
your own public key to `cloud-init.yml` and will error if
`ssh_authorized_keys` is absent. SSH uses whichever keys are available
in your agent or default identity files:

```yaml
users:
  - name: migrant
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... you@host
```

#### Remote commands

Arguments after `--` are passed through as a remote command:

```bash
migrant ssh -- sudo cloud-init status --wait
migrant ssh -- sudo tail -f /var/log/cloud-init-output.log
```

`migrant ip` prints the VM's IP address, which is useful for
scripting or for connecting with tools other than SSH.

### storage

`migrant storage` can be run from any directory, with or without a
`Migrantfile`. It lists everything in `IMAGES_DIR`, grouped by category:

```console
$ migrant storage
Directory: /var/lib/libvirt/images (16.1G)
Base Images:
  Arch-Linux-x86_64-cloudimg.qcow2 (519M)
  debian-13-generic-amd64.qcow2 (648M)
  ubuntu-25.10-server-cloudimg-amd64.img (785M)
VMs:
  arch-claude (2.4G):
    disk:     arch-claude.qcow2 (911M)
    seed iso: arch-claude-seed.iso (372K)
    snapshot: arch-claude-snapshot.qcow2 (1.5G)
  debian-claude (3.8G):
    disk:     debian-claude.qcow2 (987M)
    seed iso: debian-claude-seed.iso (372K)
    snapshot: debian-claude-snapshot.qcow2 (2.9G)
  ubuntu-claude (4.1G):
    disk:     ubuntu-claude.qcow2 (1.1G)
    seed iso: ubuntu-claude-seed.iso (372K)
    snapshot: ubuntu-claude-snapshot.qcow2 (3.1G)
Other:
  someone-elses-vm.qcow2 (2.0G)
```

`(destroyed)` means the VM's files are still on disk but the VM no longer
exists in libvirt. `migrant destroy` removes both the libvirt domain and
its image files, so this should not normally occur — it typically means the
VM was undefined directly with `virsh undefine`, or the files were left
behind after some other manual intervention. They are safe to remove.

Files in the **Other** category are not managed by migrant — they may
belong to VMs defined outside of migrant, or be leftover files from
other tools.

---

## Disk images and caching

All VM-related files are stored in `/var/lib/libvirt/images/`:

| File       | Example                                  | Purpose                                              |
| ---------- | ---------------------------------------- | ---------------------------------------------------- |
| Base image | `ubuntu-25.10-server-cloudimg-amd64.img` | Shared read-only backing file; downloaded once       |
| VM disk    | `claude.qcow2`                           | Per-VM qcow2 overlay (copy-on-write over base image) |
| Seed ISO   | `claude-seed.iso`                        | cloud-init data for first-boot provisioning          |
| Snapshot   | `claude-snapshot.qcow2`                  | Flattened disk image saved by `migrant snapshot`  |

The qcow2 overlay means:

- Creating a VM is fast — only changed blocks are written to the VM's
  own disk
- The base image is never modified
- Multiple VMs can share the same base image simultaneously
- `migrant destroy` deletes the VM's disk, seed ISO, and snapshot;
  the base image remains
- `migrant reset` also deletes the disk and seed ISO but preserves
  the snapshot, then calls `up` to rebuild from it

To free the base image:

```bash
rm /var/lib/libvirt/images/ubuntu-25.10-server-cloudimg-amd64.img
```

It will be re-downloaded next time a VM using that image is created.

---

## Security notes

The isolation guarantee in this setup comes from the KVM hypervisor
boundary, not from Linux user permissions inside the guest. The guest
`migrant` user having passwordless sudo is acceptable because:

- Privilege escalation inside the guest cannot cross the KVM boundary
- The VM is ephemeral and designed to be destroyed and rebuilt
- The shared folder is served by `virtiofsd` on the host side — the
  guest cannot influence the host filesystem beyond the shared directory

### Network isolation

Network isolation is enabled by default for all VMs. Set `NETWORK_ISOLATION=false`
in a `Migrantfile` to opt out. When active, iptables rules are added that:

- Block the VM from initiating new connections to the host (DNS and DHCP
  responses from the host are still delivered, as those are tracked as
  existing connections)
- Block the VM from reaching RFC 1918 addresses on the local network,
  other than the libvirt subnet itself (192.168.200.0/24)
- Drop all IPv6 from the VM at the `FORWARD` chain (the libvirt network
  provides no routable IPv6 to VMs; this makes that de-facto limitation
  explicit)

The rules are removed automatically when the VM stops or is destroyed.
This requires `migrant setup` to have been run to install the libvirt
hook.

### Host access rules

The `HOST_ACCESS` array in a `Migrantfile` declares exceptions to network
isolation. Each entry is a directive that the libvirt hook translates to
an iptables rule, applied atomically alongside the isolation rules:

```bash
HOST_ACCESS=(
  "allow-host-port tcp/8080"        # VM can reach host:8080
  "allow-host-port udp/5353"        # VM can reach host:5353/udp
  "allow-lan-host 192.168.1.50"     # VM can reach a specific LAN host
)
```

| Directive | Effect |
| --------- | ------ |
| `allow-host-port <proto/port>` | Allow the VM to connect to the specified host port |
| `allow-lan-host <ip>` | Allow the VM to reach a specific host on the local network |

`allow-host-port` inserts an ACCEPT rule in the per-VM INPUT chain
before the blanket REJECT. `allow-lan-host` inserts an ACCEPT in the
FORWARD chain before the RFC 1918 REJECT rules. Both are removed
automatically when the VM stops.

`HOST_ACCESS` has no effect when isolation is disabled (`NETWORK_ISOLATION=false`) —
there is nothing to poke holes in.

Combined with [lifecycle hooks](#lifecycle-hooks), this enables
host-side service patterns: a hook starts a systemd service before the
VM boots, `HOST_ACCESS` opens the port, and a hook stops the service
when the VM shuts down.

### Shared folder isolation

By default, the shared folder is backed by a fixed-size ext4 loop image
(`workspace.img` alongside your `Migrantfile`). This provides two
protections:

- **Symlink traversal prevention**: the image is mounted with the
  `nosymfollow` kernel flag. Host processes — your shell, editors, file
  watchers — cannot follow symlinks that the VM planted inside the share
  to reach files elsewhere on the host (e.g. `~/.ssh`, `/etc/passwd`).
  The flag is enforced at the VFS level and cannot be bypassed from
  userspace. `virtiofsd` itself is already safe due to its `pivot_root`
  sandbox, but this protects all other host processes.

- **Disk exhaustion prevention**: the image has a fixed size set by
  `SHARED_FOLDER_SIZE_GB` in the `Migrantfile` (default: 10 GB). The
  guest cannot write more than this cap. The image is sparse — actual
  host disk usage starts at ~67 MB and grows with contents; the full cap
  is never paid upfront.

The loop image is mounted automatically by the QEMU hook when the VM
starts, and unmounted when it stops. While the VM is halted, the
workspace files are inside the image and not directly accessible on the
host. To access them:

```bash
migrant mount    # mounts workspace.img → workspace/ (requires sudo)
# ... read, write, copy files in workspace/ ...
migrant unmount  # unmounts (requires sudo)
```

`migrant mount` can also be used to pre-populate the workspace before
the first `migrant up`.

To opt out of the loop image and use a plain host directory instead, set
`SHARED_FOLDER_ISOLATION=false` in the `Migrantfile`. This restores the
pre-loop-image behaviour (no size cap, no symlink protection) and is
appropriate only if you trust the VM's workload.

Add `*.img` to `.gitignore` to avoid committing the loop image to source
control. The `e2fsprogs` package (`mkfs.ext4`) must be installed on the
host; it is standard on all Linux distributions.

---

## WireGuard VPN tunnel

Place a standard `wireguard.conf` (wg-quick format) alongside the
`Migrantfile` to route all VM traffic through a WireGuard VPN. No
changes to `cloud-init.yml` or the VM are required.

```
ubuntu/
├── Migrantfile
├── cloud-init.yml
└── wireguard.conf      ← drop any wg-quick config here
```

`migrant up` validates the config and syncs it to a root-owned
directory (`/etc/migrant/<vm-name>/`) before starting the VM. The hook
brings up the tunnel as part of VM startup and tears it down when the
VM stops.

**Requirements:**

- `wireguard-tools` (`wg`) must be installed on the host
- The `wireguard` kernel module must be available (`modprobe wireguard`)
- `Endpoint` must be a numeric IP address, not a hostname

### How it works

The host creates a WireGuard interface (`mg-wg-<hash>`) and a dedicated
routing table. An iptables `mangle PREROUTING` rule marks every packet
arriving from the VM's tap device with the table ID; a policy rule
(`ip rule`) then diverts those marked packets to the WireGuard table,
where the only route is `default dev mg-wg-<hash>`. The result: all VM
traffic exits the host via the encrypted WireGuard tunnel, regardless
of what the VM itself does.

IPv6 from the VM is dropped at the `FORWARD` chain (shared with the
network isolation rule). The fwmark routing is IPv4-only; without
this rule IPv6 would bypass the tunnel.

`migrant up` verifies the tunnel is active before returning. If the
WireGuard interface or routing rule is missing, or the marking rule was
not applied within 5 seconds, `up` halts the VM and exits with an
error so the VM never runs un-tunneled.

### DNS

DNS behaviour depends on whether `wireguard.conf` contains a `DNS =`
line:

- **With `DNS =`**: migrant intercepts all DNS traffic from the VM
  with a `nat PREROUTING` DNAT rule and rewrites the destination to the
  VPN's DNS server. The VM continues to believe it is talking to the
  libvirt resolver (`192.168.200.1`); conntrack reverses the
  translation on the reply. DNS queries reach the VPN server through
  the tunnel and are never seen by the host resolver.

- **Without `DNS =`**: a warning is printed and DNS falls back to the
  host's resolver via libvirt's dnsmasq. Queries are not tunneled.

`migrant status` shows which DNS mode is active:

```
tunnel:     active
  iface:    mg-wg-a1b2c3d
  peer:     198.51.100.1
  dns:      10.8.0.1
```

### Threat model

The WireGuard tunnel is enforced entirely on the host, in kernel space.
The VM cannot bypass it:

- The routing policy is applied to packets leaving the VM's tap
  interface before they reach any user-space process. An attacker with
  root inside the VM cannot remove or modify these host-side rules.
- DNS interception via DNAT is also host-side. The VM cannot make DNS
  queries to an off-tunnel resolver by targeting a different IP — all
  port-53 traffic is rewritten.
- If the tunnel fails to come up, `migrant up` halts the VM rather
  than letting it run un-tunneled.
- IPv6 is blocked at the FORWARD chain (shared with the network isolation rule) so there is no IPv6 leak path.

This does not prevent the VM from sending traffic to other hosts on the
VPN once the tunnel is active. Network isolation is enabled by default
alongside WireGuard, which restricts which VPN destinations the VM can
reach (note: `NETWORK_ISOLATION` blocks RFC 1918 ranges, which do not
apply inside a VPN tunnel).

### Security notes

`wireguard.conf` contains a WireGuard private key. Keep it out of
source control:

```gitignore
*/wireguard.conf
```

The managed config directory (`/etc/migrant/<vm-name>/`) is
owner-only (`700`) so other users in the `libvirt` group cannot read
each other's private keys. The qemu hook runs as root and is
unaffected by these permissions.

---

## Lifecycle hooks

Place executable scripts in a `hooks/` directory alongside the
`Migrantfile` to run host-side actions at VM state transitions:

```
ubuntu/
├── Migrantfile
├── cloud-init.yml
├── playbook.yml
└── hooks/
    ├── pre-up        ← runs before the VM starts
    ├── post-up       ← runs after the VM is fully ready
    ├── pre-down      ← runs before the VM shuts down
    └── post-down     ← runs after the VM has stopped
```

Hooks are executable files — any language works. They run as the invoking
user (not root), so they follow the same privilege model as `migrant`
itself. Missing or non-executable hooks are silently skipped.

### Hook semantics

Hooks are tied to **state transitions**, not commands. `pre-down` and
`post-down` fire from every code path that stops the VM — `halt`,
`snapshot`, `destroy`, and `reset` — so host-side cleanup always
happens regardless of which command initiated the shutdown.

| Hook        | When it fires                                                    | Abort on failure? |
| ----------- | ---------------------------------------------------------------- | ----------------- |
| `pre-up`    | Before `virsh start` or `virt-install`                           | Yes               |
| `post-up`   | After the VM is fully ready (IP, SSH, provisioning all complete) | No (warning)      |
| `pre-down`  | Before graceful shutdown or force-stop                           | Graceful only     |
| `post-down` | After the VM has fully stopped                                   | No (warning)      |

A `pre-up` hook that exits non-zero aborts `up` before the VM starts.
A `pre-down` hook that exits non-zero aborts `halt` and `snapshot`, but
not `destroy` or `reset` — intentional destruction is not blockable by
a hook.

### Environment variables

Each hook receives these variables in its environment:

| Variable          | Description                                                       |
| ----------------- | ----------------------------------------------------------------- |
| `MIGRANT_VM_NAME` | VM name from the Migrantfile                                      |
| `MIGRANT_VM_DIR`  | Absolute path to the VM directory                                 |
| `MIGRANT_HOOK`    | Hook name (`pre-up`, `post-up`, `pre-down`, `post-down`)          |
| `MIGRANT_TRIGGER` | Command that caused this hook (`up`, `halt`, `snapshot`, `destroy`, `reset`) |
| `MIGRANT_VM_IP`   | VM IP address (set when available; empty for `pre-up` and console-only `post-up`) |

All `Migrantfile` variables (`VM_NAME`, `RAM_MB`, `NETWORKS`, etc.) are
also present in the environment, since the `Migrantfile` is sourced
before hooks run.

### Example: host-side service lifecycle

Start an inference server on the host before the VM boots, stop it when
the VM shuts down:

```bash
#!/usr/bin/env bash
# hooks/pre-up — start lemonade server for NPU workloads
systemctl --user start lemonade.service
```

```bash
#!/usr/bin/env bash
# hooks/post-down — stop lemonade when no VM needs it
systemctl --user stop lemonade.service
```

### Security notes

Hooks run as the user who invoked `migrant`, not as root. If a hook
needs privileged operations (e.g. managing firewall rules, binding
devices), use `sudo` within the hook script — this is the same model
as `migrant mount` and `migrant wg`.

Hooks are stored in the VM directory alongside the `Migrantfile`.
Because the `Migrantfile` itself is sourced as bash with no sandboxing,
hooks do not widen the trust boundary — any code in `hooks/` could
equally be placed in the `Migrantfile`.

---

## Firmware (BIOS vs UEFI)

By default, VMs use BIOS firmware (SeaBIOS). Setting `BOOT_FIRMWARE=uefi`
in a Migrantfile switches to UEFI (OVMF):

```bash
BOOT_FIRMWARE=uefi
```

**When to use this:** the Debian generic cloud image requires UEFI. Its BIOS
GRUB uses a VBE framebuffer; `--graphics none` removes the VGA device entirely,
so the kernel hangs on framebuffer initialisation before any serial output
appears. UEFI avoids this by using EFI GOP instead of VBE and falling back
gracefully to serial-only when no display is present.

Ubuntu's BIOS GRUB handles a missing VGA device correctly and does not need
this setting. Arch does not need it either — its `archlinux` osinfo-db entry
already enables UEFI automatically.

---

## Migrating an existing VM to the loop image

If you have an existing VM created before the loop image was introduced
(i.e., `workspace/` is a plain host directory with no `workspace.img`),
`destroy` is not required. The VM definition is reused as-is:

```bash
# 1. Re-run setup to install the new shared folder hook
migrant setup

# 2. Halt the VM if it is running
migrant halt

# 3. Move workspace contents out
mv workspace/ ~/workspace-backup/

# 4. Start the VM — this creates workspace.img, mounts it, then starts
migrant up

# 5. Copy files into the now-mounted workspace/
cp -a ~/workspace-backup/. workspace/
```

Alternatively, pre-populate the image before starting the VM:

```bash
migrant halt
mv workspace/ ~/workspace-backup/
migrant mount            # creates workspace.img and mounts it
cp -a ~/workspace-backup/. workspace/
migrant unmount
migrant up
```
