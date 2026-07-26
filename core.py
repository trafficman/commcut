import os
import platform
import subprocess
import json

def get_binary_path(binary_name):
    """Dynamically resolves the path to bundled binaries based on OS."""
    system = platform.system().lower()
    
    # Map OS to folder structure
    if system == 'windows':
        folder = os.path.join('bin', 'win')
        ext = '.exe'
    elif system == 'linux':
        folder = os.path.join('bin', 'linux')
        ext = ''
    elif system == 'darwin': # macOS
        folder = os.path.join('bin', 'mac')
        ext = ''
    else:
        raise EnvironmentError(f"Unsupported operating system: {system}")

    # Construct absolute path relative to this script
    base_dir = os.path.dirname(os.path.abspath(__file__))
    binary_path = os.path.join(base_dir, folder, f"{binary_name}{ext}")

    if not os.path.exists(binary_path):
        raise FileNotFoundError(f"Could not find {binary_name} at expected path: {binary_path}")

    return binary_path

def probe_file(input_path):
    """Runs ffprobe on a video file and returns its metadata as a dictionary."""
    ffprobe_path = get_binary_path('ffprobe')
    
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        input_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error probing file: {e.stderr}")
        return None

# --- Quick Test Block ---
if __name__ == "__main__":
    try:
        ffmpeg_bin = get_binary_path('ffmpeg')
        ffprobe_bin = get_binary_path('ffprobe')
        print(f"Success! Found FFmpeg at: {ffmpeg_bin}")
        print(f"Success! Found FFprobe at: {ffprobe_bin}")
    except Exception as e:
        print(f"Setup Error: {e}")