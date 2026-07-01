"""Gated tools that drive the meeting recorder (design §8)."""

from __future__ import annotations

from autobot import permissions
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.meeting.recorder import MeetingRecorder
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("meeting")


class MeetingTools:
    """Start/stop/pause/resume + status/list/summarize, backed by the recorder."""

    def __init__(self, recorder: MeetingRecorder) -> None:
        self._rec = recorder

    def start_meeting(self, title: str = "") -> str:
        """Begin recording the meeting."""
        return self._rec.start(title)

    def stop_meeting(self) -> str:
        """Stop, transcribe, summarize, and save the meeting."""
        return self._rec.stop()

    def pause_meeting(self) -> str:
        """Pause capture."""
        return self._rec.pause()

    def resume_meeting(self) -> str:
        """Resume capture."""
        return self._rec.resume()

    def meeting_status(self) -> str:
        """Report whether a meeting is recording and for how long."""
        try:
            st = self._rec.status()
            if not st["active"]:
                return "No meeting is recording right now."
            mins = int(float(st["elapsed_s"]) // 60)  # type: ignore[arg-type]
            extra = " (paused)" if st["paused"] else (" — mic-only" if st["mic_only"] else "")
            return f'Recording "{st["title"]}" for about {mins} min{extra}.'
        except Exception as exc:
            _log.exception("meeting_status failed: %s", exc)
            return "I couldn't check the meeting status right now."

    def list_meetings(self) -> str:
        """List recent saved meetings."""
        recent = self._rec.list_recent()
        if not recent:
            return "You have no saved meetings yet."
        names = [
            f'"{m.get("title") or m.get("id") or "meeting"}" ({m.get("state") or "?"})'
            for m in recent[:10]
        ]
        return "Recent meetings: " + "; ".join(names) + "."

    def summarize_meeting(self, id: str = "") -> str:
        """Rebuild minutes for a saved meeting (the most recent if ``id`` omitted)."""
        return self._rec.resummarize(id or None)

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs (relevance-gated; not core)."""
        return [
            ToolSpec(
                name="start_meeting",
                description=(
                    "Start recording the current meeting/call (Google Meet, Zoom, any "
                    "app) to take minutes. Captures both your microphone and the other "
                    "participants' audio, on-device. Cues: 'take minutes', 'record this "
                    "meeting', 'start recording the call'. Optional `title` names it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Optional meeting name.",
                        }
                    },
                    "required": [],
                },
                handler=self.start_meeting,
                risk=Risk.WRITE,
                requires=permissions.MICROPHONE,
                ack="Starting the recording.",
            ),
            ToolSpec(
                name="stop_meeting",
                description=(
                    "Stop the in-progress meeting recording, then transcribe and "
                    "summarize it. Cues: 'stop recording', 'end the meeting', 'finish "
                    "taking minutes', 'wrap up the call'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.stop_meeting,
                risk=Risk.WRITE,
                ack="Wrapping up and writing the minutes.",
            ),
            ToolSpec(
                name="pause_meeting",
                description=(
                    "Pause the meeting recording (e.g. for a private aside). "
                    "Cue: 'pause recording'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.pause_meeting,
                risk=Risk.WRITE,
                ack="Pausing.",
            ),
            ToolSpec(
                name="resume_meeting",
                description=("Resume a paused meeting recording. Cue: 'resume recording'."),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.resume_meeting,
                risk=Risk.WRITE,
                ack="Resuming.",
            ),
            ToolSpec(
                name="meeting_status",
                description=(
                    "Say whether a meeting is being recorded and for how long. "
                    "Cue: 'are you recording?'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.meeting_status,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="list_meetings",
                description=(
                    "List recent recorded meetings and their state. "
                    "Cue: 'what meetings have you saved?'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_meetings,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="summarize_meeting",
                description=(
                    "Rebuild the minutes for a saved meeting from its transcript (the "
                    "most recent if no id given). Cue: 'summarize the last meeting again'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Optional meeting id/folder name.",
                        }
                    },
                    "required": [],
                },
                handler=self.summarize_meeting,
                risk=Risk.WRITE,
                ack="Rebuilding the minutes.",
            ),
        ]


def register_meeting_tools(registry: ToolRegistry, recorder: MeetingRecorder) -> MeetingTools:
    """Register the meeting tools into ``registry``."""
    tools = MeetingTools(recorder)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("meeting tools registered")
    return tools
