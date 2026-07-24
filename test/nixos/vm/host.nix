# A disposable NixOS test VM for the migrant module: boots directly in QEMU
# (`nix run '.#test-vm'`) with nested KVM, so a real agent VM boots inside it.
{ pkgs, ... }:
{
  virtualisation.migrant.enable = true;
  virtualisation.migrant.users = [ "nixos" ];

  users.users.nixos = {
    isNormalUser = true;
    extraGroups = [ "wheel" ];
    initialPassword = "nixos";
  };
  services.getty.autologinUser = "nixos";
  security.sudo.wheelNeedsPassword = false;

  # Nested KVM: -cpu host exposes the host CPU (with svm/vmx) so the inner agent
  # VM runs accelerated rather than under TCG.
  boot.kernelModules = [
    "kvm-amd"
    "kvm-intel"
  ];
  virtualisation = {
    memorySize = 6144;
    cores = 4;
    diskSize = 20480;
    graphics = false;
    qemu.options = [ "-cpu host" ];
    # Pass the host's libvirt image cache through (9p) so the agent VM reuses an
    # already-downloaded base image instead of pulling ~530MB inside the guest.
    # The agent Migrantfile points IMAGE_URL at file:///mnt/host-images/...
    sharedDirectories.hostImages = {
      source = "/var/lib/libvirt/images";
      target = "/mnt/host-images";
    };
    # A TPM so systemd can set up service credentials — without it libvirtd.service
    # fails with status=243/CREDENTIALS in this bare qemu-vm (not a module issue;
    # real hosts and the nixosTest have working credential setup).
    tpm.enable = true;
  };

  # A ready-to-run agent VM in the user's home, plus a launcher that injects the
  # managed SSH key into its cloud-init and runs `migrant up`.
  environment.systemPackages = [
    pkgs.vim
    pkgs.curl
    (pkgs.writeShellScriptBin "agent-up" ''
      set -euo pipefail
      # libvirtd is socket-activated; make sure it's fully up so virt-install
      # doesn't race it right after boot.
      sudo systemctl start libvirtd.service
      # Base image comes from the host's store via the 9p share (see
      # sharedDirectories.hostImages); fetch it there once if absent.
      img=/mnt/host-images/Arch-Linux-x86_64-cloudimg.qcow2
      if [ ! -f "$img" ]; then
        echo "base image not in the host's store — downloading it there (once)..."
        curl -fL --retry 3 -o "$img" \
          https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2
      fi
      cd "$HOME/agent-vm"
      # Regenerate cloud-init.yml from the pristine template each run so key
      # injection stays idempotent.
      key=$(migrant pubkey)
      awk -v k="$key" '{ if ($0 ~ /SSH_PLACEHOLDER/) print "      - " k; else print }' \
        cloud-init.yml.in > cloud-init.yml
      exec migrant up
    '')
  ];

  # Seed the agent VM into the user's home. `deps = [ "users" ]` ensures this
  # runs after the home directory exists (otherwise the copy races it).
  system.activationScripts.seedAgentVm = {
    deps = [ "users" ];
    text = ''
      install -d -o nixos -g users -m 0755 /home/nixos
      if [ ! -e /home/nixos/agent-vm ]; then
        cp -rT ${./agent-vm} /home/nixos/agent-vm
        chown -R nixos:users /home/nixos/agent-vm
        chmod -R u+w /home/nixos/agent-vm
      fi
    '';
  };

  users.motd = ''

    migrant NixOS test VM (auto-logged in as nixos). Try:
      migrant setup     # doctor: verify the module wired the host
      agent-up          # launch an isolated agent VM (migrant up)
    then: migrant status | migrant ssh | migrant console | migrant destroy
  '';

  networking.hostName = "migrant-test";
  system.stateVersion = "26.05";
}
