# audio.py - Background TTS Parallel Pipeline Engine
import os
import subprocess
import json
import queue
import uuid
import re
import threading
import glob
import shutil

try:
    from prompt_toolkit.application import run_in_terminal
except ImportError:
    def run_in_terminal(func): func()

CONFIG_FILE = os.path.expanduser("~/gemini_local_agent/.cli_config.json")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        except: pass
    return {"speech_enabled": False}

def save_config():
    """Persist speech_enabled WITHOUT touching any other key in the shared config file
    (finish_action, finish_sound, pause_media, auto_transfer_on_rate_limit, ...)."""
    try:
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f: cfg = json.load(f)
                if not isinstance(cfg, dict): cfg = {}
            except Exception:
                cfg = {}
        cfg["speech_enabled"] = SPEECH_ENABLED
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, 'w') as f: json.dump(cfg, f)
        os.replace(tmp, CONFIG_FILE)
    except Exception: pass

SPEECH_ENABLED = load_config().get("speech_enabled", False)

def toggle_speech():
    global SPEECH_ENABLED
    SPEECH_ENABLED = not SPEECH_ENABLED
    save_config()
    return SPEECH_ENABLED

# Audio Engine Configuration
CANDIDATE_BASE_DIRS = [
    os.path.expanduser("~/.local/share/bigfish/audio"),
    "/opt/bigfish/audio",
]

BASE_DIR = None
PIPER_BIN = None
VOICE_MODEL = None
ESPEAK_DATA = None

for c_dir in CANDIDATE_BASE_DIRS:
    p_bin = os.path.join(c_dir, "piper", "piper")
    v_mod = os.path.join(c_dir, "models", "jenny.onnx")
    if os.path.exists(p_bin) and os.path.exists(v_mod):
        BASE_DIR = c_dir
        PIPER_BIN = p_bin
        VOICE_MODEL = v_mod
        cand_espeak = os.path.join(c_dir, "piper", "espeak-ng-data")
        if os.path.exists(cand_espeak):
            ESPEAK_DATA = cand_espeak
        break

if not BASE_DIR:
    # Default fallback to primary user directory
    BASE_DIR = "/opt/bigfish/audio"
    PIPER_BIN = os.path.join(BASE_DIR, "piper", "piper")
    VOICE_MODEL = os.path.join(BASE_DIR, "models", "jenny.onnx")
    cand_espeak = os.path.join(BASE_DIR, "piper", "espeak-ng-data")
    if os.path.exists(cand_espeak):
        ESPEAK_DATA = cand_espeak

TEXT_QUEUE = queue.Queue()
AUDIO_QUEUE = queue.Queue()
GENERATING_AUDIO = False
PLAYING_AUDIO = False
CURRENT_GENERATOR_PROC = None
CURRENT_PLAYBACK_PROC = None

# Stateful Epoch tracking to prevent phantom playbacks after stopping or session resets
SPEECH_EPOCH = 0
SPEECH_LOCK = threading.Lock()

def _get_audio_player():
    """Detects the best available audio player binary on Linux/Unix systems."""
    for player in ["paplay", "pw-play", "aplay"]:
        if shutil.which(player):
            return [player, "-q"] if player == "aplay" else [player]
    return ["paplay"]

def is_speaking():
    with SPEECH_LOCK:
        return (
            GENERATING_AUDIO
            or PLAYING_AUDIO
            or not TEXT_QUEUE.empty()
            or not AUDIO_QUEUE.empty()
        )

def _tts_generator_worker():
    global GENERATING_AUDIO, CURRENT_GENERATOR_PROC
    while True:
        item = TEXT_QUEUE.get()
        if item is None:
            break
        
        text, epoch_id = item
        
        # Fast exit if this task was cancelled before generation started
        with SPEECH_LOCK:
            if epoch_id != SPEECH_EPOCH:
                TEXT_QUEUE.task_done()
                continue
        
        GENERATING_AUDIO = True
        
        if not all(p and os.path.exists(p) for p in [PIPER_BIN, VOICE_MODEL]):
            run_in_terminal(lambda: print("\n[Audio Error: Piper engine or voice model not found.]"))
            GENERATING_AUDIO = False
            TEXT_QUEUE.task_done()
            continue
            
        temp_wav = f"/tmp/gemini_speech_{uuid.uuid4().hex}.wav"
        
        try:
            cmd = [PIPER_BIN, "--model", VOICE_MODEL, "--length_scale", "0.9", "--output_file", temp_wav]
            if ESPEAK_DATA and os.path.exists(ESPEAK_DATA):
                cmd.extend(["--espeak_data", ESPEAK_DATA])
            
            with SPEECH_LOCK:
                if epoch_id != SPEECH_EPOCH:
                    TEXT_QUEUE.task_done()
                    GENERATING_AUDIO = False
                    continue
                CURRENT_GENERATOR_PROC = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            
            try:
                CURRENT_GENERATOR_PROC.communicate(input=text.encode("utf-8"), timeout=15)
                ret_code = CURRENT_GENERATOR_PROC.returncode
            except subprocess.TimeoutExpired:
                CURRENT_GENERATOR_PROC.kill()
                CURRENT_GENERATOR_PROC.wait()
                ret_code = -1
                run_in_terminal(lambda: print("\n[Audio Warning: Piper generation process timed out.]"))
            except Exception:
                ret_code = -1
            finally:
                with SPEECH_LOCK:
                    CURRENT_GENERATOR_PROC = None

            # Re-verify epoch validity in case of cancellation during Piper compilation
            with SPEECH_LOCK:
                is_valid = (epoch_id == SPEECH_EPOCH)
                
            if is_valid and ret_code == 0 and os.path.exists(temp_wav) and os.path.getsize(temp_wav) > 0:
                AUDIO_QUEUE.put((temp_wav, epoch_id))
            else:
                try:
                    if os.path.exists(temp_wav):
                        os.remove(temp_wav)
                except Exception:
                    pass
        except Exception as e:
            run_in_terminal(lambda: print(f"\n[Audio Crash: {e}]"))
        finally:
            GENERATING_AUDIO = False
            TEXT_QUEUE.task_done()

def _tts_playback_worker():
    global PLAYING_AUDIO, CURRENT_PLAYBACK_PROC
    player_cmd = _get_audio_player()
    while True:
        item = AUDIO_QUEUE.get()
        if item is None:
            break
        
        wav_path, epoch_id = item
        
        # Verify epoch validity before launching playback binary
        with SPEECH_LOCK:
            is_valid = (epoch_id == SPEECH_EPOCH)
            
        if not is_valid:
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except Exception:
                pass
            AUDIO_QUEUE.task_done()
            continue
            
        PLAYING_AUDIO = True
        try:
            with SPEECH_LOCK:
                if epoch_id != SPEECH_EPOCH:
                    if os.path.exists(wav_path):
                        try:
                            os.remove(wav_path)
                        except Exception:
                            pass
                    PLAYING_AUDIO = False
                    AUDIO_QUEUE.task_done()
                    continue
                CURRENT_PLAYBACK_PROC = subprocess.Popen(
                    player_cmd + [wav_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            CURRENT_PLAYBACK_PROC.wait()
        except Exception as e:
            run_in_terminal(lambda: print(f"\n[Audio Playback Crash: {e}]"))
        finally:
            with SPEECH_LOCK:
                CURRENT_PLAYBACK_PROC = None
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except Exception:
                pass
            PLAYING_AUDIO = False
            AUDIO_QUEUE.task_done()

# Start both threads simultaneously
threading.Thread(target=_tts_generator_worker, daemon=True).start()
threading.Thread(target=_tts_playback_worker, daemon=True).start()

def stop_audio():
    global GENERATING_AUDIO, PLAYING_AUDIO, CURRENT_GENERATOR_PROC, CURRENT_PLAYBACK_PROC, SPEECH_EPOCH
    
    # Invalidate all current in-flight speech transactions
    with SPEECH_LOCK:
        SPEECH_EPOCH += 1
        
    # Terminate active generator subprocess immediately
    if CURRENT_GENERATOR_PROC:
        try:
            CURRENT_GENERATOR_PROC.terminate()
            CURRENT_GENERATOR_PROC.kill()
        except Exception:
            pass
        CURRENT_GENERATOR_PROC = None

    # Terminate active audio playback subprocess immediately
    if CURRENT_PLAYBACK_PROC:
        try:
            CURRENT_PLAYBACK_PROC.terminate()
            CURRENT_PLAYBACK_PROC.kill()
        except Exception:
            pass
        CURRENT_PLAYBACK_PROC = None

    # Drain text queue
    while not TEXT_QUEUE.empty():
        try:
            TEXT_QUEUE.get_nowait()
            TEXT_QUEUE.task_done()
        except (queue.Empty, ValueError):
            break

    # Drain audio queue and unlink temporary WAV files
    while not AUDIO_QUEUE.empty():
        try:
            item = AUDIO_QUEUE.get_nowait()
            if item and item[0] and os.path.exists(item[0]):
                try:
                    os.remove(item[0])
                except Exception:
                    pass
            AUDIO_QUEUE.task_done()
        except (queue.Empty, ValueError):
            break

    if os.name != 'nt':
        os.system("pkill -9 paplay 2>/dev/null; pkill -9 pw-play 2>/dev/null; pkill -9 aplay 2>/dev/null; pkill -9 piper 2>/dev/null")
    else:
        os.system("taskkill /F /IM paplay.exe /IM pw-play.exe /IM aplay.exe /IM piper.exe >nul 2>&1")
        
    # Native, OS-agnostic temporary WAV file cleanup
    for f in glob.glob("/tmp/gemini_speech_*.wav"):
        try:
            os.remove(f)
        except Exception:
            pass
    
    GENERATING_AUDIO = False
    PLAYING_AUDIO = False

def split_into_sentences(text):
    """Splits filtered text into individual sentences for sequential TTS generation and queued playback."""
    if not text or not text.strip():
        return []
    
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    
    # Common abbreviations to protect from premature sentence splitting
    abbrevs = [
        r"\be\.g\.", r"\bi\.e\.", r"\betc\.", r"\bvs\.", r"\bDr\.", r"\bMr\.", r"\bMrs\.", r"\bMs\.", 
        r"\bProf\.", r"\bInc\.", r"\bLtd\.", r"\bJr\.", r"\bSr\.", r"\bapprox\.", r"\bdept\.", r"\bfig\.",
        r"\bno\.", r"\bvol\.", r"\brev\.", r"\best\."
    ]
    protected = text
    placeholder_map = {}
    for i, pattern in enumerate(abbrevs):
        def repl(match):
            key = f"__ABBR_{i}_{len(placeholder_map)}__"
            placeholder_map[key] = match.group(0)
            return key
        protected = re.sub(pattern, repl, protected, flags=re.IGNORECASE)
    
    # Protect numbered list markers (e.g. "1. ", " 2. ")
    def list_repl(match):
        key = f"__LISTNUM_{len(placeholder_map)}__"
        placeholder_map[key] = match.group(0)
        return key
    protected = re.sub(r"(?:^|(?<=\s))\d+\.(?=\s)", list_repl, protected)

    # Protect numbers with decimals, e.g. 1.5, 3.14, v1.0
    def num_repl(match):
        key = f"__NUM_{len(placeholder_map)}__"
        placeholder_map[key] = match.group(0)
        return key
    protected = re.sub(r"\b\d+\.\d+\b", num_repl, protected)
    protected = re.sub(r"\bv\d+\.\d+\b", num_repl, protected, flags=re.IGNORECASE)

    # Protect ellipses (...)
    def ellipse_repl(match):
        key = f"__ELLIPSE_{len(placeholder_map)}__"
        placeholder_map[key] = match.group(0)
        return key
    protected = re.sub(r"\.{3,}", ellipse_repl, protected)

    # Split on sentence boundaries: (. ! ?) followed by whitespace or quotes/parens, or newlines
    raw_chunks = re.split(r"(?<=[.!?])\s+|(?<=[.!?][\"'’])\s+|\n+", protected)
    
    sentences = []
    for chunk in raw_chunks:
        # Restore protected placeholders
        for key, orig in placeholder_map.items():
            chunk = chunk.replace(key, orig)
        chunk = chunk.strip()
        if not chunk:
            continue
            
        # If a single chunk is abnormally long (> 200 chars), split gracefully at clause boundaries
        if len(chunk) > 200:
            sub_parts = re.split(r"(?<=[,;:])\s+", chunk)
            cur = ""
            for sp in sub_parts:
                if cur and len(cur) + len(sp) > 160:
                    sentences.append(cur.strip())
                    cur = sp
                else:
                    cur = f"{cur} {sp}" if cur else sp
            if cur.strip():
                sentences.append(cur.strip())
        else:
            sentences.append(chunk)
            
    return sentences

def filter_text_for_speech(text):
    """Filters out code blocks, XML tags, LaTeX math markup, and non-spoken syntax for clean TTS rendering."""
    if not text:
        return ""
    
    # 1. Strip raw XML tags and termination markers
    # New format: tags inside say text are shown as written; nothing is stripped here.
    text = text.replace('__DONE__', '')
    
    # 2. Remove multi-line code blocks completely
    text = re.sub(r'```[\s\S]*?```', '', text)
    
    # 3. Strip inline code backticks: `var_name` -> var name
    text = re.sub(r'`([^`]+)`', r'\1', text)
    
    # 4. Clean LaTeX math notations into natural spoken words
    text = re.sub(r'\\approx', ' approximately ', text)
    text = re.sub(r'\\times', ' times ', text)
    text = re.sub(r'\\cdot', ' times ', text)
    text = re.sub(r'\\pm', ' plus or minus ', text)
    text = re.sub(r'\\le|\\leq', ' less than or equal to ', text)
    text = re.sub(r'\\ge|\\geq', ' greater than or equal to ', text)
    text = re.sub(r'\\neq', ' not equal to ', text)
    text = re.sub(r'\\mathbf\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathit\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1 over \2', text)
    text = text.replace('$', '')
    
    # 5. Clean URLs, emojis, and markdown structural characters
    text = re.sub(r'https?://\S+|www\.\S+', 'a web link', text)
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)  # Emojis
    text = re.sub(r'[\u2600-\u27BF]', '', text)
    text = re.sub(r'^[#*_\-\|]+\s*', '', text, flags=re.MULTILINE)
    text = text.replace('**', '').replace('*', '').replace('#', '').replace('|', ' ')
    
    # 6. Normalize whitespace: preserve line breaks for natural sentence splitting
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r'\n+', '\n', text).strip()
    return text

def speak(text):
    if not text or not text.strip():
        return
    
    clean_text = filter_text_for_speech(text)
    if not clean_text or not clean_text.strip():
        return
    
    sentences = split_into_sentences(clean_text)
    if not sentences:
        return
        
    # Reset any lingering prior audio playback, queues, or background tasks
    stop_audio()
    
    # Establish new epoch for this speech session
    global SPEECH_EPOCH
    with SPEECH_LOCK:
        SPEECH_EPOCH += 1
        current_epoch = SPEECH_EPOCH
        
    for sentence in sentences:
        TEXT_QUEUE.put((sentence, current_epoch))
