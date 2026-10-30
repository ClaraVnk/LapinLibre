# SPDX-License-Identifier: MIT
"""tools/app-server/asr_server.py's recognition ("recognise"): the mu-law audio, the upsampling, the
match of the words heard to the grammar's alternatives, and whole sessions through Parakeet v3 and
Silero VAD over TCP, played as voice-daemon plays them (8 kHz mu-law in 1024-byte chunks, with the
speech of the model's own test recordings in German, English, Spanish and French).

The sessions need sherpa-onnx (tools/app-server's environment) and the model files
(asr_server.py --fetch); without them those tests are skipped. From tools/app-server:

    uv run python -I -B ../../tests/app_server/test_asr_server.py
"""

import socket
import struct
import sys
import threading
import time
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "app-server"))
import asr_server  # noqa: E402

TEST_WAVS = asr_server.MODELS / asr_server.PARAKEET / "test_wavs"
SENTENCES = {
    "de": "Alles hat ein Ende, nur die Wurst hat zwei",
    "en": "Ask not what your country can do for you, ask what you can do for your country",
    "es": "No preguntes qué puede hacer tu país por ti, pregunta qué puedes hacer tú por tu país",
    "fr": "Ne vous demandez pas ce que votre pays peut faire pour vous, demandez-vous plutôt ce que "
          "vous pouvez faire pour lui",
}


def mulaw_byte(x):
    """G.711 mu-law of one 16-bit sample (the reference encoder)."""
    sign = 0x80 if x < 0 else 0
    x = min(abs(x), 32635) + 0x84
    exp = 7
    while exp > 0 and not x & (0x4000 >> (7 - exp)):
        exp -= 1
    return ~(sign | exp << 4 | (x >> (exp + 3)) & 0x0F) & 0xFF


def rabbit_audio(path):
    """A WAV as voice-daemon would send it: 8000 samples a second (each the nearest), mu-law."""
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        pcm = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
    return bytes(mulaw_byte(pcm[int(i * rate / 8000)]) for i in range(int(len(pcm) * 8000 / rate)))


SILENCE = bytes([0xFF]) * 8000  # one second of mu-law zeros


class AudioTest(unittest.TestCase):
    def test_mulaw_table(self):
        self.assertEqual((asr_server.MULAW[0xFF], asr_server.MULAW[0x7F]), (0, 0))
        self.assertEqual((asr_server.MULAW[0x80], asr_server.MULAW[0x00]), (32124, -32124))
        for x in (-30000, -1000, -100, -5, 0, 5, 100, 1000, 30000):
            got = asr_server.MULAW[mulaw_byte(x)]
            self.assertLessEqual(abs(got - x), max(4, abs(x) // 16), x)

    def test_decode_and_upsample(self):
        self.assertEqual(asr_server.decode_audio(bytes([0xFF, 0x80])), [0.0, 32124 / 32768])
        out, last = asr_server.upsample([1.0, 0.0, -1.0], last=0.5)
        self.assertEqual((out, last), ([0.75, 1.0, 0.5, 0.0, -0.5, -1.0], -1.0))


class MatchTest(unittest.TestCase):
    FOUND = [("hello rabbit", "<app>hello</app>"), ("good night", ""), ("Device test", "<app>devicetest</app>")]

    def test_normalise(self):
        self.assertEqual(asr_server.normalise("Plutôt, ÇA va?  Très bien!"), ["plutot", "ca", "va", "tres", "bien"])

    def test_closest_alternative_with_its_semantic(self):
        self.assertEqual(asr_server.best_match("Hello, rabbit.", self.FOUND), ("hello rabbit", "<app>hello</app>", 1.0))
        words, semantic, sim = asr_server.best_match("device tests", self.FOUND)
        self.assertEqual((words, semantic, sim), ("Device test", "<app>devicetest</app>", 0.5))

    def test_alternative_inside_a_longer_phrase(self):
        self.assertEqual(asr_server.best_match("um, good night everyone", self.FOUND)[2], 0.9)

    def test_nothing_close(self):
        self.assertLess(asr_server.best_match("what is the weather", self.FOUND)[2], asr_server.MATCH_MIN)
        self.assertIsNone(asr_server.best_match("anything", []))
        self.assertEqual(asr_server.similarity("", "hello"), 0.0)


def recogniser_or_skip():
    try:
        return asr_server.Recogniser()
    except RuntimeError as e:
        raise unittest.SkipTest(str(e))


class SessionTest(unittest.TestCase):
    """Whole sessions through the model, over TCP, as voice-daemon plays them."""

    @classmethod
    def setUpClass(cls):
        asr_server.Session.recogniser = recogniser_or_skip()
        cls.server = asr_server.asr_server("127.0.0.1", 0, "recognise")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def session(self, grammar, audio):
        """The grammar, then the audio in 1024-byte chunks (then silence) until answered: the answer's
        fields, and the seconds of audio sent before it."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        with sock:
            def send(payload):
                sock.sendall(struct.pack(">I", len(payload)) + payload)
            send(asr_server.length_field(1, grammar.encode()))
            audio += SILENCE * 12
            sock.setblocking(False)
            answer, sent = b"", 0
            deadline = time.time() + 60
            while time.time() < deadline and sent < len(audio):
                send(asr_server.length_field(2, audio[sent:sent + 1024]))
                sent += 1024
                try:
                    answer += sock.recv(4096)
                except BlockingIOError:
                    pass
                if len(answer) >= 4 and len(answer) >= 4 + struct.unpack(">I", answer[:4])[0]:
                    break
                time.sleep(0.002)
            sock.setblocking(True)
            sock.settimeout(10)
            while len(answer) < 4 or len(answer) < 4 + struct.unpack(">I", answer[:4])[0]:
                answer += sock.recv(4096)
        message = answer[4:4 + struct.unpack(">I", answer[:4])[0]]
        response = next(v for n, _, v in asr_server.fields(message) if n == 3)
        return {n: v for n, _, v in asr_server.fields(response)}, sent / 8000

    def grammar(self, alternatives):
        return ("#ABNF 1.0 UTF-8;\r\nlanguage en-US;\r\nmode voice;\r\ntag-format <semantics/1.0>;\r\nroot $App;\r\n"
                "public $App = " + " | ".join(alternatives) + ";\r\n")

    def test_each_language_answers_its_own_sentence(self):
        alternatives = [f"{s} {{<app>{lang}</app>}}" for lang, s in SENTENCES.items()] + ["good night", "hello rabbit"]
        for lang in SENTENCES:
            with self.subTest(lang):
                audio = rabbit_audio(TEST_WAVS / f"{lang}.wav")
                fields, sent = self.session(self.grammar(alternatives), SILENCE + audio)
                self.assertEqual(fields[1], 1)  # OK
                self.assertEqual(fields[3].decode(), SENTENCES[lang])
                self.assertEqual(fields[4].decode(), f"<app>{lang}</app>")
                self.assertGreaterEqual(fields[2], 80)

    def test_the_end_of_the_speech_comes_after_the_silence(self):
        # The detector reports the speech once SILENCE_END_S of silence follows it, not before the
        # speaker stops, with all the speech in it.
        audio = rabbit_audio(TEST_WAVS / "en.wav")
        listening = asr_server.Listening(asr_server.Session.recogniser)
        stream = SILENCE + audio + SILENCE * 5
        for at in range(0, len(stream), 1024):
            heard = listening.feed(stream[at:at + 1024])
            if heard is not None:
                break
        self.assertEqual(heard[0], "speech")
        end_of_speech = (len(SILENCE) + len(audio)) / 8000
        self.assertGreater((at + 1024) / 8000, end_of_speech)
        self.assertLess((at + 1024) / 8000, end_of_speech + asr_server.SILENCE_END_S + 1)
        self.assertGreater(len(heard[1]) / asr_server.MODEL_RATE, len(audio) / 8000 - 1)

    def test_a_short_sound_first_is_not_the_answer(self):
        # A short sound that matches nothing (here the first 0.4 s of the sentence, then silence; on
        # the rabbit, a cough or a tone) doesn't end the session: the sentence after it is answered.
        audio = rabbit_audio(TEST_WAVS / "en.wav")
        alternatives = [f"{SENTENCES['en']} {{<app>en</app>}}", "good night"]
        fields, _ = self.session(self.grammar(alternatives), SILENCE + audio[:3200] + SILENCE * 2 + audio)
        self.assertEqual((fields[1], fields[4].decode()), (1, "<app>en</app>"))

    def test_words_not_in_the_grammar_are_no_match(self):
        fields, _ = self.session(self.grammar(["good night", "hello rabbit"]), rabbit_audio(TEST_WAVS / "en.wav"))
        self.assertEqual(fields[1], 8)  # NOMATCH
        self.assertIn("country", fields[3].decode().lower())

    def test_silence_is_no_input(self):
        fields, sent = self.session(self.grammar(["hello rabbit"]), b"")
        self.assertEqual(fields[1], 9)  # NOINPUT
        self.assertAlmostEqual(sent, asr_server.NOINPUT_S, delta=0.5)


if __name__ == "__main__":
    unittest.main()
