#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A speech-recognition server for the rabbit's voice-daemon (voice.conf [asr]).

voice-daemon connects over TCP, sends the grammar (an ABNF text, or the URL
of one: the controller's launch grammar, /asr/grammar/<id>.abnf), then the
microphone's audio in chunks, each an AsrMsg (userland/proto/asr-message.proto)
preceded by its length (4 bytes, network order), and waits for an AsrMsg with
a Response (code, words, semantic, score). What it answers, for each session:

  "recognise"    what was said: the grammar's alternative closest to it
                 (code OK), or NOMATCH with the words heard, or NOINPUT when
                 nobody speaks (needs sherpa-onnx and the model files)
  "first"        the grammar's first alternative (the default)
  "nomatch"      no match (code NOMATCH)
  any text       that text as the recognised words

with the semantic the grammar gives the answered alternative ({<app>key</app>
<par>instance</par>}: what the controller starts an application from). A
grammar given by URL comes from set_grammar_source's function when the app
server runs this one (app_server.py --asr-port), or is fetched.

The audio is 8-bit mu-law, mono, 8000 samples a second: voice-daemon reads
/dev/audio at 16 kHz and sends every second byte. With "recognise", Silero VAD
(sherpa-onnx's) finds the end of the speech: the answer comes SILENCE_END_S
after the speaker stops, at most SPEECH_MAX_S after the speech starts, or
NOINPUT_S without any speech; Parakeet TDT 0.6B v3 (int8, on the CPU,
25 European languages, found from the audio) makes the words. A short sound
that matches nothing (a cough, the long-press tone) isn't answered: the server
listens on, until NOINPUT_S from the start. The other
answers come once AFTER_S seconds of audio have arrived (the rabbit keeps
streaming until it is answered). The grammar and the audio received are
logged; the audio of the last session is kept as asr-last.raw (as sent) and
asr-last.wav next to the uploads when a folder is given.

    python3 asr_server.py --port 8443 [--answer recognise|first|nomatch|TEXT]
    python3 asr_server.py --fetch          the model files, into --models

Only "recognise" needs a package (sherpa-onnx, uv sync in this folder); the
rest is the standard library (the message is small enough to encode by hand).
"""

import argparse
import hashlib
import re
import socketserver
import struct
import tarfile
import threading
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

AFTER_S = 1.5
CODES = {"OK": 1, "ERROR": 2, "TIMEOUT": 3, "TERMINATED": 4, "CANCELLED": 5, "BUSY": 6, "COMPFAILURE": 7,
         "NOMATCH": 8, "NOINPUT": 9}

# The audio as voice-daemon sends it, and as the detector and the model take it (twice the samples,
# each new one between two old ones).
RATE = 8000
MODEL_RATE = 16000
# Recognition: the silence that ends the speech, the longest speech taken, the time without speech
# that is NOINPUT, the speech too short to answer when it matches nothing (all in seconds of audio),
# the detector's threshold (the rabbit's microphone is quiet: Silero's default of 0.5 cut the start
# of a word on the rabbit), and the lowest similarity that answers an alternative.
SILENCE_END_S = 0.7
SPEECH_MAX_S = 8.0
NOINPUT_S = 8.0
SHORT_S = 0.6
VAD_THRESHOLD = 0.35
MATCH_MIN = 0.6
# The model files, and where --fetch gets them (k2-fsa/sherpa-onnx's releases), each with its SHA-256.
MODELS = Path.home() / ".cache" / "lapinlibre" / "asr"
PARAKEET = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_SHA256 = {
    f"{PARAKEET}/encoder.int8.onnx": "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247",
    f"{PARAKEET}/decoder.int8.onnx": "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e",
    f"{PARAKEET}/joiner.int8.onnx": "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3",
    f"{PARAKEET}/tokens.txt": "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
    "silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
DOWNLOADS = {
    f"{PARAKEET}.tar.bz2": "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf",
    "silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}


def log(text):
    print(time.strftime("%H:%M:%S ") + "asr: " + text, flush=True)


# ---- protobuf wire format, for AsrMsg's few fields ----

def varint(n):
    out = b""
    while True:
        byte, n = n & 0x7f, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def read_varint(data, i):
    shift = value = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def fields(data):
    """(field number, wire type, value) for each field of a message."""
    i = 0
    while i < len(data):
        key, i = read_varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = read_varint(data, i)
        elif wire == 2:
            length, i = read_varint(data, i)
            value, i = data[i:i + length], i + length
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wire}")
        yield number, wire, value


def length_field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def response_message(code, words="", semantic="", score=0):
    """An AsrMsg holding a Response."""
    response = varint(1 << 3) + varint(CODES[code]) + varint(2 << 3) + varint(score)
    if words:
        response += length_field(3, words.encode())
    if semantic:
        response += length_field(4, semantic.encode())
    return length_field(3, response)


# ---- the grammar ----

def choices(grammar):
    """(words, semantic) of each alternative of the grammar's rule ("public $App = a | b {<app>k</app>};"
    or the rule a launch grammar uses), in order; the semantic is the alternative's {...} tag, or "".
    """
    m = re.search(r"public \$\w+ = (.*?);", grammar, re.S)
    if not m:
        return []
    out = []
    for a in m.group(1).split("|"):
        if not a.strip():
            continue
        tag = re.search(r"\{([^}]*)\}", a)
        words = " ".join(re.sub(r"\{[^}]*\}|<[^>]*>", "", a).split())
        out.append((words, tag.group(1).strip() if tag else ""))
    return out


def alternatives(grammar):
    """The words of the grammar's rule's alternatives, in order."""
    return [words for words, _ in choices(grammar)]


def resolve(grammar):
    """The grammar's text: a URL is read through the grammar source (the app server's own grammar),
    else fetched; "" when it can't be."""
    if not re.fullmatch(r"https?://\S+", grammar.strip()):
        return grammar
    url = grammar.strip()
    if Session.grammar_source is not None:
        text = Session.grammar_source(url)
        if text is not None:
            return text
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read(65536).decode(errors="replace")
    except OSError as e:
        log(f"grammar {url}: {e}")
        return ""


def normalise(text):
    """The words of a text, for comparing: lower case, without accents or punctuation."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text).split()


def word_distance(a, b):
    """The edit distance between two lists of words (insertions, deletions, replacements)."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[len(b)]


def similarity(heard, alternative):
    """How close the words heard are to an alternative's, from 0 to 1. An alternative said whole
    inside a longer phrase counts 0.9."""
    a, b = normalise(heard), normalise(alternative)
    if not a or not b:
        return 0.0
    s = 1 - word_distance(a, b) / max(len(a), len(b))
    if len(b) < len(a) and any(a[i:i + len(b)] == b for i in range(len(a) - len(b) + 1)):
        s = max(s, 0.9)
    return s


def best_match(heard, found):
    """(words, semantic, similarity) of the grammar's alternative closest to the words heard, or None
    when the grammar has none."""
    scored = [(similarity(heard, w), w, s) for w, s in found]
    if not scored:
        return None
    sim, words, semantic = max(scored, key=lambda t: t[0])
    return words, semantic, sim


# ---- the audio ----

def mulaw_sample(u):
    """The linear value (-32124 to 32124) of one G.711 mu-law byte."""
    u = ~u & 0xFF
    x = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    return 0x84 - x if u & 0x80 else x - 0x84


MULAW = [mulaw_sample(u) for u in range(256)]


def decode_audio(data):
    """The samples of mu-law bytes, as floats from -1 to 1."""
    return [MULAW[b] / 32768.0 for b in data]


def upsample(samples, last=0.0):
    """Samples at RATE as samples at MODEL_RATE: each one preceded by the mean of it and the one
    before (`last` for the first), and the new last sample."""
    out = []
    for s in samples:
        out.append((last + s) / 2)
        out.append(s)
        last = s
    return out, last


def write_wav(path, data):
    """mu-law bytes as a 16-bit WAV at RATE."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(data)}h", *(MULAW[b] for b in data)))


# ---- recognition ----

def check_models(models):
    """RuntimeError unless every model file is in `models` with its SHA-256."""
    for name, want in MODEL_SHA256.items():
        path = models / name
        if not path.is_file():
            raise RuntimeError(f"no {path} (asr_server.py --fetch gets it)")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != want:
            raise RuntimeError(f"{path}: not the expected file (sha256)")


class Recogniser:
    """Parakeet TDT 0.6B v3 and Silero VAD through sherpa-onnx, loaded once and shared by the sessions;
    RuntimeError when sherpa-onnx or a checked model file is missing."""

    def __init__(self, models=MODELS, threads=4):
        try:
            import sherpa_onnx
        except ImportError:
            raise RuntimeError("sherpa-onnx is not installed (uv sync in this folder)") from None
        models = Path(models)
        check_models(models)
        self.sherpa, self.models = sherpa_onnx, models
        m = models / PARAKEET
        started = time.monotonic()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(m / "encoder.int8.onnx"), decoder=str(m / "decoder.int8.onnx"),
            joiner=str(m / "joiner.int8.onnx"), tokens=str(m / "tokens.txt"), num_threads=threads,
            model_type="nemo_transducer", provider="cpu")
        self.lock = threading.Lock()
        log(f"Parakeet v3 loaded ({time.monotonic() - started:.1f} s)")

    def listener(self):
        """A new voice-activity detector, for one session, and its window (in samples)."""
        config = self.sherpa.VadModelConfig()
        config.silero_vad.model = str(self.models / "silero_vad.onnx")
        config.silero_vad.min_silence_duration = SILENCE_END_S
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.threshold = VAD_THRESHOLD
        config.silero_vad.max_speech_duration = SPEECH_MAX_S
        config.sample_rate = MODEL_RATE
        vad = self.sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=SPEECH_MAX_S + NOINPUT_S + 5)
        return vad, config.silero_vad.window_size

    def transcribe(self, samples):
        """The words in the samples (at MODEL_RATE)."""
        with self.lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(MODEL_RATE, samples)
            self.recognizer.decode_stream(stream)
            return stream.result.text.strip()


class Listening:
    """One session's audio through the voice-activity detector. feed() takes mu-law bytes as they
    come and returns ("speech", samples) once the speech has ended (or reached SPEECH_MAX_S),
    ("noinput", None) after NOINPUT_S without speech, or None to wait for more."""

    def __init__(self, recogniser):
        self.vad, self.window = recogniser.listener()
        self.pending = []
        self.last = 0.0           # the last sample, for the upsampling
        self.samples = 0          # the samples given to the detector so far (at MODEL_RATE)
        self.speech_from = None   # where the speech started, in samples

    def feed(self, data):
        more, self.last = upsample(decode_audio(data), self.last)
        self.pending += more
        while len(self.pending) >= self.window:
            self.vad.accept_waveform(self.pending[:self.window])
            del self.pending[:self.window]
            self.samples += self.window
            if self.speech_from is None and self.vad.is_speech_detected():
                self.speech_from = self.samples
            if not self.vad.empty():
                return "speech", self.segment()
        if self.speech_from is None and self.samples >= NOINPUT_S * MODEL_RATE:
            return "noinput", None
        if self.speech_from is not None and self.samples - self.speech_from >= (SPEECH_MAX_S + 1) * MODEL_RATE:
            self.vad.flush()
            if not self.vad.empty():
                return "speech", self.segment()
            return "noinput", None
        return None

    def segment(self):
        samples = list(self.vad.front.samples)
        self.vad.pop()
        return samples

    def again(self):
        """Listen on after a segment that isn't answered: NOINPUT_S still counts from the start."""
        self.speech_from = None


# ---- the server ----

class Session(socketserver.BaseRequestHandler):
    answer = "first"            # the class's setting, changed by set_answer()
    grammar_source = None       # URL -> grammar text (or None), set by set_grammar_source()
    keep: "Path | None" = None  # where to keep the last session's audio, or None
    recogniser: "Recogniser | None" = None  # the shared Recogniser, once "recognise" is asked for
    models = MODELS

    def recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def handle(self):
        peer = self.client_address[0]
        log(f"session from {peer}")
        grammar, audio, started, answered = "", bytearray(), None, False
        listening = Listening(Session.recogniser) if Session.answer == "recognise" else None
        self.request.settimeout(30)
        try:
            while True:
                size = struct.unpack(">I", self.recv_exact(4))[0]
                message = self.recv_exact(size)
                chunk = b""
                for number, _, value in fields(message):
                    if number == 1:
                        grammar = value.decode(errors="replace")
                        log(f"grammar: {' '.join(grammar.split())[:200]}")
                    elif number == 2:
                        audio += value
                        chunk += value
                        started = started or time.time()
                if answered:
                    continue
                if listening is not None:
                    heard = listening.feed(chunk) if chunk else None
                    if heard is not None:
                        answer = self.recognised(grammar, *heard, final=listening.samples >= NOINPUT_S * MODEL_RATE)
                        if answer is None:
                            listening.again()
                        else:
                            self.request.sendall(answer)
                            answered = True
                elif started and time.time() - started >= AFTER_S:
                    self.request.sendall(self.reply(grammar))
                    answered = True
        except (EOFError, OSError):
            pass
        log(f"session ended: {len(audio)} bytes of audio ({len(audio) / RATE:.1f} s)")
        if Session.keep and audio:
            (Session.keep / "asr-last.raw").write_bytes(bytes(audio))
            write_wav(Session.keep / "asr-last.wav", audio)

    @staticmethod
    def framed(payload):
        return struct.pack(">I", len(payload)) + payload

    def recognised(self, grammar, kind, samples, final=True):
        """The answer to what the listener heard: the closest alternative, NOMATCH or NOINPUT; None
        (listen on) for speech without words, or shorter than SHORT_S and matching nothing, unless the
        session is past NOINPUT_S (final)."""
        if kind == "noinput":
            log("answering: no input (nobody spoke)")
            return self.framed(response_message("NOINPUT"))
        started = time.monotonic()
        heard = Session.recogniser.transcribe(samples)
        took = time.monotonic() - started
        log(f"heard {heard!r} ({len(samples) / MODEL_RATE:.1f} s of speech, {took:.2f} s)")
        found = choices(resolve(grammar))
        match = best_match(heard, found)
        short = len(samples) / MODEL_RATE < SHORT_S and (match is None or match[2] < MATCH_MIN)
        if not final and (not heard or short):
            log("listening on (a sound, not an answer)")
            return None
        if match is None:
            if not heard:
                return self.framed(response_message("NOMATCH"))
            log(f"answering: {heard!r} (the grammar has no alternatives)")
            return self.framed(response_message("OK", words=heard, score=80))
        words, semantic, sim = match
        if sim < MATCH_MIN:
            log(f"answering: no match (closest {words!r}, {sim:.2f})")
            return self.framed(response_message("NOMATCH", words=heard))
        log(f"answering: {words!r}" + (f" ({semantic})" if semantic else "") + f", {sim:.2f}")
        return self.framed(response_message("OK", words=words, semantic=semantic, score=round(sim * 100)))

    def reply(self, grammar):
        answer = Session.answer
        if answer == "nomatch":
            log("answering: no match")
            payload = response_message("NOMATCH", score=0)
        else:
            found = choices(resolve(grammar))
            if answer == "first":
                words, semantic = found[0] if found else ("", "")
            else:
                words = answer
                semantic = next((s for w, s in found if w.lower() == answer.lower()), "")
            log(f"answering: {words!r}" + (f" ({semantic})" if semantic else ""))
            payload = response_message("OK", words=words, semantic=semantic, score=80)
        return self.framed(payload)


def set_answer(answer):
    """What the sessions answer from now on; "recognise" loads the recogniser first (RuntimeError
    when it can't be)."""
    if answer == "recognise" and Session.recogniser is None:
        Session.recogniser = Recogniser(Session.models)
    Session.answer = answer


def set_grammar_source(source):
    """source(url): the grammar text for a grammar given by URL, or None to fetch it."""
    Session.grammar_source = source


def asr_server(listen, port, answer="first", keep=None, models=MODELS):
    Session.keep, Session.models = Path(keep) if keep else None, Path(models)
    set_answer(answer)
    server = socketserver.ThreadingTCPServer((listen, port), Session)
    server.daemon_threads = True
    return server


def fetch(models=MODELS):
    """Downloads the model files into `models` (each checked by its SHA-256, the archive unpacked
    without links or paths outside it), then checks them."""
    models = Path(models)
    models.mkdir(parents=True, exist_ok=True)
    for name, want in DOWNLOADS.items():
        part = models / (name + ".part")
        log(f"fetching {RELEASES}{name}")
        h = hashlib.sha256()
        with urllib.request.urlopen(RELEASES + name, timeout=60) as r, part.open("wb") as f:
            for block in iter(lambda: r.read(1 << 20), b""):
                h.update(block)
                f.write(block)
        if h.hexdigest() != want:
            part.unlink()
            raise RuntimeError(f"{name}: not the expected file (sha256)")
        if name.endswith(".tar.bz2"):
            with tarfile.open(part) as t:
                t.extractall(models, filter="data")
            part.unlink()
        else:
            part.replace(models / name)
    check_models(models)
    log(f"the model files are in {models}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--answer", default="first",
                   help='"recognise" (the model), "first" (default), "nomatch" or the words to answer')
    p.add_argument("--keep", metavar="DIR", help="keep the last session's audio in DIR/asr-last.raw and .wav")
    p.add_argument("--models", type=Path, default=MODELS, help="the model files (default: %(default)s)")
    p.add_argument("--fetch", action="store_true", help="download the model files into --models, then stop")
    a = p.parse_args()
    if a.fetch:
        fetch(a.models)
        return
    server = asr_server(a.listen, a.port, a.answer, a.keep, a.models)
    log(f"listening on {a.listen}:{a.port}, answering {a.answer!r}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
