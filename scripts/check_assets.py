#!/usr/bin/env python3
import sys
import json
import os
import subprocess
import argparse

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["post_invocation", "stop", "claude"], required=True)
    return parser.parse_args()

def get_workspace():
    # If stdin has data (Antigravity sends it), try to parse
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
            if stdin_data:
                input_data = json.loads(stdin_data)
                if "workspacePaths" in input_data and input_data["workspacePaths"]:
                    return input_data["workspacePaths"][0]
        except Exception:
            pass
    return "."

def get_image_files(workspace):
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=workspace,
            text=True,
            stderr=subprocess.DEVNULL
        )
        all_files = output.strip().split("\n")
    except Exception:
        all_files = []

    return [f for f in all_files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]

def get_ledger_content(workspace):
    ledger_path = None
    # Just do a quick walk to find the first assets_ledger.json
    for root, dirs, files in os.walk(workspace):
        # Ignore .git or node_modules
        dirs[:] = [d for d in dirs if d not in [".git", "node_modules", ".agents", ".venv"]]
        if "assets_ledger.json" in files:
            ledger_path = os.path.join(root, "assets_ledger.json")
            break
            
    if ledger_path:
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return ""

def main():
    args = parse_args()
    workspace = get_workspace()
    
    image_files = get_image_files(workspace)
    if not image_files:
        if args.mode == "stop":
            print(json.dumps({"decision": "allow"}))
        elif args.mode == "post_invocation":
            print(json.dumps({})) # Empty object means nothing to inject
        elif args.mode == "claude":
            sys.exit(0)
        return

    ledger_content = get_ledger_content(workspace)
    
    missing_images = []
    for img in image_files:
        basename = os.path.basename(img)
        if basename not in ledger_content:
            missing_images.append(img)

    if not missing_images:
        if args.mode == "stop":
            print(json.dumps({"decision": "allow"}))
        elif args.mode == "post_invocation":
            print(json.dumps({}))
        elif args.mode == "claude":
            sys.exit(0)
        return

    # Format output based on mode
    reason = f"The following image files are missing from the assets ledger: {', '.join(missing_images)}. You MUST log them using the log_ai_asset tool."
    
    if args.mode == "stop":
        print(json.dumps({
            "decision": "continue",
            "reason": reason
        }))
    elif args.mode == "post_invocation":
        print(json.dumps({
            "injectSteps": [
                {
                    "ephemeralMessage": f"IMMEDIATE ACTION REQUIRED: {reason} Do this now while your generation context/prompt is still fresh!"
                }
            ]
        }))
    elif args.mode == "claude":
        # Claude might not parse Antigravity's JSON, so we just print to stderr and exit with code 1
        print(f"HOOK FAILED: {reason}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
