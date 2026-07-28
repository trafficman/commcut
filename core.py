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

def get_video_specs(input_path):
    """
    Runs ffprobe to collect and print vital video specs:
    duration, resolution, framerate, and codec.
    """
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
        data = json.loads(result.stdout)
        
        # Extract file format info
        format_info = data.get("format", {})
        duration = float(format_info.get("duration", 0))
        
        # Find the first video stream for resolution/fps/codec
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        
        specs = {
            "filename": os.path.basename(input_path),
            "duration": duration,
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "codec": video_stream.get("codec_name"),
            "fps": video_stream.get("r_frame_rate")
        }
        
        return specs
    except subprocess.CalledProcessError as e:
        print(f"Error reading specs: {e.stderr}")
        return None

def basic_transcode_cut(input_path, output_path, start_time, end_time):
    """
    Basic transcoding cut: takes a video, cuts from start_time to end_time,
    and outputs to the specified path. Transcodes to ensure clean cuts.
    """
    ffmpeg_path = get_binary_path('ffmpeg')
    
    # Calculate duration from start and end times
    duration = end_time - start_time
    
    cmd = [
        ffmpeg_path,
        "-y",               # Overwrite output file without asking
        "-ss", str(start_time),  # Seek to start time (fast seek before input)
        "-i", input_path,
        "-t", str(duration),     # Duration of the clip
        "-c:v", "libx264",  # Standard video transcode
        "-c:a", "aac",      # Standard audio transcode
        output_path
    ]
    
    print(f"Executing cut: {start_time}s to {end_time}s ({duration}s duration)...")
    
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"Successfully exported to: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg cutting error:\n{e.stderr}")

# --- Quick Test Block ---
if __name__ == "__main__":
    # Define paths for your dev environment
    import_dir = "import"
    export_dir = "export"
    
    os.makedirs(import_dir, exist_ok=True)
    os.makedirs(export_dir, exist_ok=True)
    
    # Drop a test video into your 'import' folder and name it 'test.mp4'
    input_file = os.path.join(import_dir, "test.mp4")
    output_file = os.path.join(export_dir, "output_clip.mp4")
    
    if os.path.exists(input_file):
        # 1. Test Spec Collection
        specs = get_video_specs(input_file)
        print("\n--- Video Specs ---")
        for key, value in specs.items():
            print(f"{key}: {value}")
            
        # 2. Test Basic Cut (e.g., cut from second 5.0 to second 15.0)
        print("\n--- Running Test Cut ---")
        basic_transcode_cut(input_file, output_file, start_time=30.8, end_time=60.9)
    else:
        print(f"Please place a test video named 'test.mp4' inside your '{import_dir}' folder to run the test.")