import os
import requests
import re
import subprocess
import sys

# --- Configuration ---
SOURCES = {
    "anddea": {
        "patches_repo": "anddea/revanced-patches",
        "cli_repo": "MorpheApp/morphe-cli", 
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": False
    },
    "morphe": {
        "patches_repo": "MorpheApp/morphe-patches",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": False
    },

    "morphe-dev": {
        "patches_repo": "MorpheApp/morphe-patches",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": True
    },
    "piko": {
        "patches_repo": "crimera/piko",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": False
    },
    "piko-dev": {
        "patches_repo": "crimera/piko",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": True
    },
    "de-revanced": {
        "patches_repo": "RookieEnough/De-ReVanced",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": False
    },

    "hoo-dles": {
        "patches_repo": "hoo-dles/morphe-patches",
        "cli_repo": "MorpheApp/morphe-cli",
        "patches_asset": ".mpp",
        "cli_asset": ".jar",
        "use_prerelease": False
    }
}

APPS_TO_CHECK = [
    "com.google.android.youtube",
    "com.google.android.apps.youtube.music",
    "com.reddit.frontpage",
    "com.twitter.android",
    "com.spotify.music",
    "com.google.android.apps.photos",
    "ch.protonvpn.android"
]

HEADERS = {"User-Agent": "Mozilla/5.0"}
github_token = os.environ.get("GITHUB_TOKEN")
if github_token:
    HEADERS["Authorization"] = f"token {github_token}"

def download_asset(repo, extension, output_dir, allow_prerelease=False):
    if allow_prerelease:
        url = f"https://api.github.com/repos/{repo}/releases"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        
    try:
        resp = requests.get(url, headers=HEADERS).json()
        
        # If we asked for all releases, pick the first one
        if allow_prerelease and isinstance(resp, list) and len(resp) > 0:
            resp = resp[0]
            
        if 'assets' not in resp:
            api_msg = resp.get('message', 'No specific error message provided')
            print(f"Error: No assets found for {repo} | API Response: {api_msg}")
            return None
            
        for asset in resp['assets']:
            if asset['name'].endswith(extension) and "source" not in asset['name']:
                # Prefer 'all.jar' for CLI to avoid dependency issues
                if extension == ".jar" and "all" not in asset['name'] and any("all" in a['name'] for a in resp['assets']):
                    continue
                
                download_url = asset['browser_download_url']
                filename = os.path.join(output_dir, asset['name'])
                if not os.path.exists(filename):
                    print(f"Downloading {asset['name']} from {repo}...")
                    with requests.get(download_url, stream=True) as r:
                        r.raise_for_status()
                        with open(filename, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                return filename
    except Exception as e:
        print(f"Error fetching {repo}: {e}")
    return None

def check_versions():
    os.makedirs("tools_check", exist_ok=True)
    
    print(f"{'Source':<18} | {'App Package':<40} | {'Compatible Versions'}")
    print("-" * 120)

    for source_name, config in SOURCES.items():
        cli_path = download_asset(config["cli_repo"], config["cli_asset"], "tools_check", config.get("use_prerelease", False))
        patches_path = download_asset(config["patches_repo"], config["patches_asset"], "tools_check", config.get("use_prerelease", False))

        if not cli_path or not patches_path:
            print(f"{source_name:<18} | ERROR: Could not download tools")
            continue

        try:         
            # Universal syntax logic
            cmd = ["java", "-jar", cli_path, "list-versions", f"--patches={patches_path}"]         
            process = subprocess.run(cmd, capture_output=True, text=True)
            
            output = process.stdout
            
            # --- Parsing Logic ---
            found_versions = {}
            current_pkg = None

            for line in output.splitlines():
                line = line.strip()
                if not line: continue

                # Match "Package name: com.package.name" ignoring prefix like "INFO: "
                pkg_match = re.search(r"Package name:\s*([a-zA-Z0-9_.]+)", line)
                if pkg_match:
                    current_pkg = pkg_match.group(1)
                    continue

                if current_pkg in APPS_TO_CHECK:
                    # Ignore headers
                    if "compatible versions" in line: continue
                    
                    # Match version numbers at start of line
                    # Matches: 19.16.39 OR v19.16.39
                    v_match = re.match(r'^(v?\d+(\.\d+)+)', line)
                    
                    if v_match:
                        v = v_match.group(1)
                        if current_pkg not in found_versions:
                            found_versions[current_pkg] = []
                        if v not in found_versions[current_pkg]:
                            found_versions[current_pkg].append(v)
                    elif "Any" in line:
                         if current_pkg not in found_versions:
                             found_versions[current_pkg] = []
                         if "Any" not in found_versions[current_pkg]:
                             found_versions[current_pkg].append("Any")

            # --- Display Results ---
            for app in APPS_TO_CHECK:
                if app in found_versions and found_versions[app]:
                    vs = found_versions[app]
                    if "Any" in vs:
                         print(f"{source_name:<18} | {app:<40} | Any")
                    else:
                        # Sort versions desc
                        def sort_key(s):
                            try:
                                return [int(x) for x in s.lstrip('v').split('.')]
                            except:
                                return [0]
                        
                        vs.sort(key=sort_key, reverse=True)
                        # Join all versions with comma
                        all_versions_str = ", ".join(vs)
                        print(f"{source_name:<18} | {app:<40} | {all_versions_str}")
                else:
                    print(f"{source_name:<18} | {app:<40} | None (Not in patches)")

        except Exception as e:
            print(f"{source_name:<18} | Exception: {e}")

if __name__ == "__main__":
    check_versions()
