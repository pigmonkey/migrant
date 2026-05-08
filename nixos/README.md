# NixOS example

A minimal NixOS VM managed by migrant.

Unlike the arch, ubuntu, and debian examples, NixOS does not publish pre-built
cloud images. The qcow2 is built locally from `flake.nix` instead.

## Prerequisites

- [Nix](https://nixos.org/download/) with flakes enabled
- migrant host setup complete (`migrant setup`)

## Usage

Build the image (first time only, or after changing `flake.nix`):

    cd nixos
    nix build

Update the SSH key in `cloud-init.yml` to match your managed key:

    migrant pubkey

Copy the output into the `ssh_authorized_keys` field in `cloud-init.yml`.

Start the VM:

    migrant up

## What's in the image

Defined in `flake.nix` (NixOS 25.11):

- cloud-init (NoCloud datasource)
- OpenSSH
- Serial console on ttyS0
- virtiofs kernel support
- `/bin/bash` symlink (NixOS only provides `/bin/sh` by default)
- git, unzip
- Nix flakes enabled
- Unnecessary services disabled (fstrim, nix-gc, nix-optimise)

## What cloud-init configures

Defined in `cloud-init.yml` (per-instance, applied at first boot):

- User `migrant` with passwordless sudo
- Managed SSH key
- Shared folder mounted at `/home/migrant/workspace` via virtiofs
- cloud-init disabled after first boot

## Differences from the other examples

NixOS is declarative, which changes how provisioning works:

- **Image is built locally** — `nix build` instead of downloading a cloud image.
- **No `playbook.yml`** — all system configuration is baked into the image via
  `flake.nix`. There is nothing to install after boot.
- **fstab is read-only** — NixOS generates `/etc/fstab` from its configuration,
  so the virtiofs mount uses an explicit `mount -t virtiofs` command in
  cloud-init `runcmd` rather than appending to fstab.
- **Unnecessary services disabled in the flake** — no systemd masking needed
  at provision time.

## Rebuilding the image

If you change `flake.nix`, rebuild and recreate the VM:

    nix build
    migrant destroy
    migrant up

Changes to `cloud-init.yml` also require a destroy/up cycle since
cloud-init only runs on first boot.
