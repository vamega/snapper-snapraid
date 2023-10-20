{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.pre-commit-hooks = {
    url = "github:cachix/pre-commit-hooks.nix";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.nixpkgs-stable.follows = "nixpkgs";
  };

  outputs = {
    self,
    nixpkgs,
    pre-commit-hooks,
  }: let
    inherit (nixpkgs) lib;
    supportedSystems = ["x86_64-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin"];
    forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    pkgs = forAllSystems (system: nixpkgs.legacyPackages.${system});
  in {
    devShells = forAllSystems (system: {
      default = pkgs.${system}.mkShellNoCC {
        packages = with pkgs.${system}; let
          pythonEnv = python3.withPackages (
            ps:
              with ps;
                [
                  requests
                  jsonschema
                  psutil
                  ipython
                  black
                ]
                ++ lib.optionals (lib.hasSuffix "linux" system) [
                  systemd
                ]
          );
        in [
          pythonEnv
          python3Packages.flit
        ];

        inherit (self.checks.${system}.pre-commit-check) shellHook;
      };
    });

    checks = forAllSystems (system: {
      pre-commit-check = pre-commit-hooks.lib.${system}.run {
        src = ./.;
        hooks = {
          black.enable = true;
        };
      };
    });
  };
}
