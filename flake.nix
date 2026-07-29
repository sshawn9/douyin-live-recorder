{
  description = "Douyin live recorder and lossless video tools";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonPackages = pkgs.python314Packages;
        in
        {
          default = pythonPackages.buildPythonApplication {
            pname = "douyin-live-recorder";
            version = "0.0.1";
            pyproject = true;

            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./README.md
                ./pyproject.toml
                ./src
              ];
            };

            build-system = [ pythonPackages.hatchling ];
            dependencies = [
              pythonPackages.httpx
              pythonPackages.typer
            ];

            nativeBuildInputs = [ pkgs.makeWrapper ];
            postInstall =
              let
                runtimePath = pkgs.lib.makeBinPath [ pkgs.ffmpeg-headless ];
              in
              ''
                wrapProgram $out/bin/douyin-live-recorder --prefix PATH : ${runtimePath}
                wrapProgram $out/bin/video-tools --prefix PATH : ${runtimePath}
              '';

            pythonImportsCheck = [
              "douyin_live_recorder"
              "video_tools"
            ];
          };
        }
      );

      apps = forAllSystems (
        system:
        let
          package = self.packages.${system}.default;
        in
        {
          default = {
            type = "app";
            program = nixpkgs.lib.getExe' package "douyin-live-recorder";
            meta.description = "Monitor and record a Douyin live stream";
          };
          video-tools = {
            type = "app";
            program = nixpkgs.lib.getExe' package "video-tools";
            meta.description = "Inspect, repair, cut, and concatenate video files";
          };
        }
      );
    };
}
