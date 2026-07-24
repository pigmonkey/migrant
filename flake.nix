{
  description = "migrant — secure ephemeral libvirt/QEMU VMs for coding agents";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      # External commands the CLI shells out to (kept in sync via checks.cli-closure).
      runtimeDeps = with pkgs; [
        libvirt
        virt-manager
        qemu_kvm
        iproute2
        wireguard-tools
        openssh
        ansible
        libisoburn
        cdrkit
        curl
        iptables
        nftables
        e2fsprogs
        findutils
        coreutils
        gnugrep
        gnused
        gawk
        util-linux
      ];

      migrant = pkgs.stdenvNoCC.mkDerivation {
        pname = "migrant";
        version = "0-unstable-2026-07-22";
        src = ./.;
        nativeBuildInputs = [ pkgs.makeWrapper ];
        dontBuild = true;
        installPhase = ''
          runHook preInstall
          install -Dm755 migrant $out/bin/migrant
          # The sandbox has no /usr/bin/env, and we run the script below in
          # installPhase, so pin the interpreter first.
          patchShebangs $out/bin/migrant
          # Single source of truth: migrant emits its own hooks/completion/xml
          # (coreutils only, no require_config — pure).
          mkdir -p $out/share/migrant
          $out/bin/migrant export $out/share/migrant
          install -Dm644 $out/share/migrant/completion/_migrant \
            $out/share/zsh/site-functions/_migrant
          wrapProgram $out/bin/migrant --prefix PATH : ${pkgs.lib.makeBinPath runtimeDeps}
          runHook postInstall
        '';
        meta = with pkgs.lib; {
          description = "Secure, ephemeral libvirt/QEMU VM manager for coding agents";
          homepage = "https://github.com/pigmonkey/migrant";
          license = licenses.unlicense;
          platforms = [ "x86_64-linux" ];
          mainProgram = "migrant";
        };
      };
    in
    {
      packages.${system} = {
        inherit migrant;
        default = migrant;

        # Disposable NixOS VM with the module enabled, booted directly in QEMU
        # (`nix run '.#test-vm'`) with nested KVM — for testing migrant on real NixOS.
        test-vm =
          (nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              "${nixpkgs}/nixos/modules/virtualisation/qemu-vm.nix"
              self.nixosModules.default
              ./test/nixos/vm/host.nix
            ];
          }).config.system.build.vm;

        # `nix run '.#test-vm-check'` — boots the same VM with a self-check that
        # runs the whole flow (doctor -> agent boot -> isolation asserts) and
        # reports PASS/FAIL via the shared dir. Reliable one-command end-to-end
        # (needs KVM + nested virt; takes a few minutes under double-nesting).
        test-vm-check =
          let
            vm =
              (nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  "${nixpkgs}/nixos/modules/virtualisation/qemu-vm.nix"
                  self.nixosModules.default
                  ./test/nixos/vm/host.nix
                  ./test/nixos/vm/selfcheck.nix
                ];
              }).config.system.build.vm;
          in
          pkgs.writeShellScriptBin "migrant-test-vm-check" ''
            set -u
            d=$(mktemp -d)
            export SHARED_DIR="$d" NIX_DISK_IMAGE="$d/disk.qcow2"
            cd "$d"   # keep the VM's swtpm state etc. in the temp dir, not the caller's CWD
            echo "Running migrant NixOS self-check (boots a real agent VM under nested KVM; a few minutes)..."
            timeout 1500 ${vm}/bin/run-migrant-test-vm </dev/null >"$d/console.log" 2>&1 || true
            res=$(cat "$d/result" 2>/dev/null || echo NO-RESULT)
            echo
            if [ "$res" = PASS ]; then
              echo "migrant NixOS self-check: PASS"
              rm -rf "$d"
            else
              echo "migrant NixOS self-check: $res"
              echo "--- console tail ---"
              tail -60 "$d/console.log"
              exit 1
            fi
          '';
      };

      devShells.${system}.default = pkgs.mkShell {
        packages =
          with pkgs;
          [
            shellcheck
            ansible-lint
            python3
            nixfmt
          ]
          ++ runtimeDeps;
      };

      nixosModules.default = import ./nix/module.nix self;

      checks.${system} = import ./test/nixos/checks.nix { inherit pkgs self migrant; };
    };
}
