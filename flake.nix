{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = {
    self,
    nixpkgs,
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
      };
    });
  };
}
