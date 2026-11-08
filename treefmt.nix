_: {
  projectRootFile = "flake.nix";

  programs = {
    nixfmt.enable = true;

    biome = {
      enable = true;
      formatCommand = "format";
      includes = [
        "*.html"
        "*.json"
      ];
      settings = {
        html.formatter.enabled = true;
      };
    };

    ruff-format.enable = true;
    ruff-check.enable = true;
  };

  settings.global.excludes = [
    ".gitignore"
    "*.lock"
    "*.md"
    "*.kicad_*"
    "**/fab/**"
    "*.svg"
  ];
}
