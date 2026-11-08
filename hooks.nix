{ inputs, system }:
let
  pkgs = inputs.nixpkgs.legacyPackages.${system};

  treefmtEval = inputs.treefmt-nix.lib.evalModule pkgs ./treefmt.nix;

  python = pkgs.python3.withPackages (ps: [ ps.pytest ]);
  pcbnewSitePackages = "${pkgs.kicad.base}/${pkgs.python3.sitePackages}";

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

      ruff = {
        enable = true;
        stages = [ "pre-push" ];
      };

      ty = {
        enable = true;
        name = "ty";
        description = "type check every python file";
        entry = "${pkgs.lib.getExe pkgs.ty} check --python ${python} --extra-search-path ${pcbnewSitePackages} --extra-search-path tool";
        files = "\\.py$";
        stages = [ "pre-push" ];
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
      python
      pkgs.ruff
      pkgs.ty
      pkgs.freerouting
    ];
    PYTHONPATH = pcbnewSitePackages;
    KICAD10_FOOTPRINT_DIR = "${pkgs.kicad.libraries.footprints}/share/kicad/footprints";
    KICAD10_SYMBOL_DIR = "${pkgs.kicad.libraries.symbols}/share/kicad/symbols";
  };
}
