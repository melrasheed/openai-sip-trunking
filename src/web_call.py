"""Web calls: the agent over the browser's microphone and speakers.

A phone call's audio travels over the SIP trunk, so the server only ever
observes it. A web call has no media path of its own: the console streams its
microphone to this app over a WebSocket, this module relays it into a Realtime
session, and relays the agent's voice back the other way.

Everything else — who the caller is, the prompt, the knowledge base, holding
phrases, the transcript and the call log — is the phone call's own code, run
unchanged. This module holds only the difference:

  1. `session_update()` turns a phone call's accept payload into the
     `session.update` a WebSocket session is configured with.
  2. `Bridge` carries audio between the browser and the service.

Wire protocol with the console (see templates/index.html):

  browser -> app   binary frames: PCM16, little-endian, mono, 24 kHz
                   {"type": "truncate", "item_id": ..., "audio_end_ms": ...}
                   {"type": "hangup"} — the server then closes the socket
  app -> browser   {"type": "web_call.started" | "web_call.connected" | "web_call.error", ...}
                   plus the service events in FORWARDED, passed through as-is
"""

import asyncio
import base64
import copy
import json
import socket
import threading
import uuid

import websockets

# The console captures and plays raw PCM at the rate WebSocket sessions use by
# default. Stated explicitly so a change of default upstream cannot quietly
# turn the call into noise.
AUDIO_FORMAT = {"type": "audio/pcm", "rate": 24000}

# A laptop or meeting-room microphone hears the whole room, where a handset
# hears only the person holding it. Filtering for that stops stray room noise
# triggering voice detection and cutting the agent off mid-sentence.
NOISE_REDUCTION = {"type": "far_field"}

# Service events the browser needs: the agent's audio, and the moments the
# caller starts and stops talking, so playback can stop when they talk over
# it. An allowlist on purpose — the session events echo the instructions and
# the knowledge base key, and must never leave this process.
FORWARDED = frozenset(
    {
        "response.output_audio.delta",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
    }
)

# What `_translate` returns when the browser asks to hang up.
_HANGUP = object()


def new_call_id():
    """An id for the call log. Phone calls use the service's `rtc_…` ids."""
    return "web_" + uuid.uuid4().hex[:21]


def session_update(body):
    """The `session.update` for a web call, built from the accept payload.

    `body` is exactly what a phone call from the same number is accepted with,
    so both channels get the same prompt, voice, tools and turn detection. The
    model is left out because a WebSocket session names its deployment in the
    URL. The payload is copied, never modified, so the phone path cannot be
    affected by what a web call adds.
    """
    session = copy.deepcopy({key: value for key, value in body.items() if key != "model"})
    audio = session.setdefault("audio", {})
    audio_input = audio.setdefault("input", {})
    audio_input["format"] = dict(AUDIO_FORMAT)
    audio_input["noise_reduction"] = dict(NOISE_REDUCTION)
    audio.setdefault("output", {})["format"] = dict(AUDIO_FORMAT)
    return {"type": "session.update", "session": session}


class Bridge:
    """Carries one web call's audio between the browser and the service.

    `ws` is the browser's socket. It is blocking, while the realtime session
    runs on asyncio, so a reader thread turns what the browser sends into a
    queue the session loop can await. `session` is the call's session config,
    applied by the monitor once the service is connected.
    """

    def __init__(self, ws, call_id, session=None):
        self.ws = ws
        self.call_id = call_id
        self.session = session
        self._open = True

    def notify(self, payload):
        """Tells the console something about the call. Never raises."""
        self._send(json.dumps(payload, ensure_ascii=False))

    def fail(self, message):
        """Tells the console why the call could not go ahead."""
        self.notify({"type": "web_call.error", "message": message})

    def forward(self, event, raw):
        """Passes a service event on to the browser if it is one it needs.

        `raw` is the message exactly as it arrived, so audio is relayed
        without being decoded and encoded again.
        """
        if event.get("type") in FORWARDED:
            self._send(raw)

    def close(self):
        """Hangs up the browser's side. Safe to call more than once."""
        self._open = False
        try:
            self.ws.close()
        except Exception:
            pass
        # Flask's development server writes an ordinary HTTP response once a
        # WebSocket route returns, and the browser, still reading frames,
        # reports it as a broken one. Half-closing the socket after the close
        # frame makes that write fail quietly instead.
        try:
            self.ws.sock.shutdown(socket.SHUT_WR)
        except (AttributeError, OSError):
            pass

    def _send(self, text):
        if not self._open:
            return
        try:
            self.ws.send(text)
        except Exception:
            # The browser has gone. The reader notices too, and ends the call.
            self._open = False

    async def pump(self, realtime):
        """Relays the browser to the service until one side goes away.

        Returns True when the browser hung up, False when the service ended
        the session first.
        """
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        def deliver(item):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # The session has already ended and its loop is gone.
                pass

        def reader():
            try:
                while True:
                    deliver(self.ws.receive())
            except Exception:
                # Closed by the browser, or by us at the end of the call.
                pass
            finally:
                deliver(None)

        threading.Thread(target=reader, name=f"web-{self.call_id}", daemon=True).start()

        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    return True
                outbound = self._translate(frame)
                if outbound is _HANGUP:
                    return True
                if outbound:
                    await realtime.send(json.dumps(outbound))
        except websockets.exceptions.ConnectionClosed:
            # The service ended the session first; the monitor handles that.
            return False

    @staticmethod
    def _translate(frame):
        """One browser frame as the service event it stands for, or None.

        Only audio, truncation and hanging up are accepted, so nothing the page
        sends can rewrite the session — its prompt and tools stay the server's.
        """
        if isinstance(frame, (bytes, bytearray)):
            if not frame:
                return None
            return {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }

        try:
            message = json.loads(frame)
        except (TypeError, ValueError):
            return None

        if not isinstance(message, dict):
            return None
        if message.get("type") == "hangup":
            return _HANGUP
        if message.get("type") != "truncate":
            return None
        item_id = message.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            return None
        try:
            audio_end_ms = max(0, int(message.get("audio_end_ms") or 0))
        except (TypeError, ValueError):
            return None

        # The caller talked over the agent. Tell the service how much of the
        # reply they actually heard, so its record of the conversation matches
        # theirs rather than assuming the whole reply was played.
        return {
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": audio_end_ms,
        }
