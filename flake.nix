{
  description = "Schedule owned short-form videos for TikTok publishing";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          shortbridge = pkgs.python3Packages.buildPythonApplication {
            pname = "shortbridge";
            version = "0.1.0";
            pyproject = true;
            src = nixpkgs.lib.cleanSource ./.;

            build-system = [ pkgs.python3Packages.hatchling ];
            dependencies = [ ];

            nativeCheckInputs = [ ];
            checkPhase = ''
              runHook preCheck
              PYTHONPATH=$PWD/src ${pkgs.python3.interpreter} -m unittest discover -s tests
              runHook postCheck
            '';
            pythonImportsCheck = [ "shortbridge" ];

            makeWrapperArgs = [
              "--prefix PATH : ${nixpkgs.lib.makeBinPath [ pkgs.yt-dlp pkgs.ffmpeg ]}"
            ];

            meta = {
              description = "Schedule owned YouTube Shorts or Instagram Reels for TikTok";
              homepage = "https://github.com/emiliopalmerini/shortbridge";
              license = nixpkgs.lib.licenses.mit;
              mainProgram = "shortbridge";
              platforms = systems;
            };
          };
        in
        {
          inherit shortbridge;
          default = shortbridge;
        });

      apps = forAllSystems (system: {
        shortbridge = {
          type = "app";
          program = "${self.packages.${system}.shortbridge}/bin/shortbridge";
          meta.description = "Schedule owned short-form videos for TikTok publishing";
        };
        default = self.apps.${system}.shortbridge;
      });
    };
}
