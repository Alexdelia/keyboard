{
  description = "icarus split Hall effect keyboard";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    pre-commit-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs:
    import ./hooks.nix {
      inherit inputs;
      system = "x86_64-linux";
    };
}
