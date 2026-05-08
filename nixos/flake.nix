{
  description = "NixOS qcow2 cloud image for migrant";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;

      # NixOS system configuration for the cloud image
      nixosConfig = lib.nixosSystem {
        inherit system;
        modules = [
          # QEMU/KVM guest profile — loads virtio drivers and guest agent
          "${nixpkgs}/nixos/modules/profiles/qemu-guest.nix"

          ({ config, pkgs, modulesPath, ... }: {

            # --- Disk image builder ---
            system.build.qcow2 = import "${modulesPath}/../lib/make-disk-image.nix" {
              inherit lib config pkgs;
              baseName = "nixos-base";    # avoid collision with migrant's {VM_NAME}.qcow2
              diskSize = "auto";          # shrink to fit; growpart expands at boot
              format = "qcow2";
              partitionTableType = "legacy";  # MBR — no EFI partition needed
            };

            # --- Boot ---
            boot.loader.grub.enable = true;
            boot.loader.grub.device = "/dev/vda";
            boot.growPartition = true;

            fileSystems."/" = {
              device = "/dev/vda1";
              fsType = "ext4";
              autoResize = true;
            };

            # --- Serial console ---
            boot.kernelParams = [ "console=ttyS0,115200n8" ];
            systemd.services."serial-getty@ttyS0".enable = true;

            # --- virtiofs ---
            # Module must be available; the actual mount is handled by
            # cloud-init runcmd (the user and homedir don't exist until
            # cloud-init creates them).
            boot.initrd.availableKernelModules = [ "virtiofs" ];

            # --- Cloud-init ---
            # SSH keys and user creation are handled by cloud-init at boot
            # (from the seed ISO that migrant generates), keeping the base
            # image generic and reusable across key changes.
            services.cloud-init.enable = true;
            # Override default cloud_final_modules to remove
            # keys-to-console: its helper (write-ssh-key-fingerprints)
            # is missing from the NixOS cloud-init package, causing a
            # recoverable error and exit code 2 instead of 0.
            # NixOS openssh generates host keys itself; stop cloud-init
            # from attempting it (fails on NixOS → exit code 2).
            services.cloud-init.settings.ssh_genkeytypes = [];
            services.cloud-init.settings.cloud_final_modules = [
              "rightscale_userdata"
              "scripts-vendor"
              "scripts-per-once"
              "scripts-per-boot"
              "scripts-per-instance"
              "scripts-user"
              "ssh-authkey-fingerprints"
              # "keys-to-console" — removed (missing helper binary on NixOS)
              "phone-home"
              "final-message"
              "power-state-change"
            ];

            # --- OpenSSH ---
            services.openssh = {
              enable = true;
              settings = {
                PermitRootLogin = "prohibit-password";
                PasswordAuthentication = true;
              };
            };

            # Allow passwordless sudo for wheel group members
            security.sudo.wheelNeedsPassword = false;

            # --- /bin/bash compatibility ---
            # NixOS only provides /bin/sh by default. cloud-init (and
            # many scripts) expect /bin/bash, so create the symlink.
            system.activationScripts.binbash = lib.stringAfter [ "stdio" ] ''
              ln -sfn ${pkgs.bash}/bin/bash /bin/bash
            '';

            # --- Nix flakes ---
            nix.settings.experimental-features = [ "nix-command" "flakes" ];

            # --- Packages ---
            environment.systemPackages = with pkgs; [
              cloud-init  # CLI must be on PATH for migrant's cloud-init status --wait
              git
              unzip
            ];

            # --- Disable unnecessary services ---
            systemd.timers.fstrim.enable = false;
            nix.gc.automatic = false;
            nix.optimise.automatic = false;

            networking.hostName = "nixos";

            system.stateVersion = "25.11";
          })
        ];
      };

    in {
      packages.${system} = let
        image = nixosConfig.config.system.build.qcow2;
      in {
        nixos-image = image;
        default = image;
      };
    };
}
