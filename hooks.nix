{ inputs, system }:
let
  pkgs = inputs.nixpkgs.legacyPackages.${system};

  treefmtEval = inputs.treefmt-nix.lib.evalModule pkgs ./treefmt.nix;

  pre-commit-check = inputs.pre-commit-hooks.lib.${system}.run {
    src = ./.;
    hooks = {
      treefmt = {
        enable = true;
        stages = [ "pre-commit" ];

        packageOverrides.treefmt = treefmtEval.config.build.wrapper;
      };

      typos = {
        enable = true;
        stages = [ "pre-commit" ];
      };

      deadnix = {
        enable = true;
        stages = [ "pre-push" ];
      };

      statix = {
        enable = true;
        stages = [ "pre-push" ];

        settings.format = "stderr";
      };
    };
  };

  pcbnewSitePackages = "${pkgs.kicad.base}/${pkgs.python3.sitePackages}";
in
{
  checks.${system}.pre-commit-check = pre-commit-check;

  formatter.${system} = treefmtEval.config.build.wrapper;

  devShells.${system}.default = pkgs.mkShell {
    shellHook = ''
      ${pre-commit-check.shellHook}
      export PYTHONPATH="$PWD/tool''${PYTHONPATH:+:$PYTHONPATH}"
    '';
    buildInputs = pre-commit-check.enabledPackages ++ [
      pkgs.kicad
      (pkgs.python3.withPackages (ps: [ ps.pytest ]))
      pkgs.freerouting
    ];
    PYTHONPATH = pcbnewSitePackages;
    KICAD10_FOOTPRINT_DIR = "${pkgs.kicad.libraries.footprints}/share/kicad/footprints";
  };
}
