run-uv user_id:
    uv run douyin-live-recorder {{ user_id }}

run-nix user_id:
    nix run . -- {{ user_id }}
