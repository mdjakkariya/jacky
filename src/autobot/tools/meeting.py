"""Gated tools that drive the meeting recorder (design §8)."""

from __future__ import annotations

from autobot import permissions
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.meeting.recorder import MeetingRecorder
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("meeting")


def _brief(minutes_md: str) -> tuple[str, str]:
    """Extract (title, summary) from a minutes.md — the H1 and the Summary paragraph.

    Pure and defensive: returns empty strings for anything it can't find. Used to
    give a spoken one-liner without dumping the whole file.

    Args:
        minutes_md: The raw minutes markdown.

    Returns:
        ``(title, summary)`` — either may be ``""``.
    """
    title = ""
    buf: list[str] = []
    in_summary = False
    for line in (minutes_md or "").splitlines():
        s = line.strip()
        if not title and s.startswith("# "):
            title = s[2:].strip()
            continue
        if s.startswith("## "):
            in_summary = s[3:].strip().lower() == "summary"
            continue
        if in_summary and s:
            buf.append(s)
    return title, " ".join(buf)


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
        """List recent saved meetings with their folder paths."""
        recent = self._rec.list_recent()
        if not recent:
            return "You have no saved meetings yet."
        lines = [
            f'· "{m.get("title") or m.get("id") or "meeting"}" ({m.get("state") or "?"}) '
            f"— {m.get('dir') or m.get('id') or ''}"
            for m in recent[:10]
        ]
        return "Your saved meetings (newest first):\n" + "\n".join(lines)

    def last_meeting(self) -> str:
        """Say where the most recent meeting is saved and give its summary."""
        try:
            last = self._rec.last_minutes()
        except Exception as exc:
            _log.exception("last_meeting failed: %s", exc)
            return "I couldn't look up your last meeting right now."
        if not last:
            return "You have no saved meetings yet."
        directory = str(last.get("dir", "")).strip()
        title, summary = _brief(str(last.get("minutes_md", "")))
        name = title or "your last meeting"
        where = f" It's saved in {directory} (minutes.md)." if directory else ""
        if summary:
            excerpt = summary if len(summary) <= 500 else summary[:500].rstrip() + "…"
            return f'Your most recent meeting is "{name}".{where} Summary: {excerpt}'
        return f'Your most recent meeting is "{name}".{where}'

    def summarize_meeting(self, id: str = "") -> str:
        """Rebuild minutes for a saved meeting (the most recent if ``id`` omitted)."""
        return self._rec.resummarize(id or None)

    def delete_meeting(self, id: str = "") -> str:
        """Permanently delete a saved meeting (the most recent if ``id`` omitted)."""
        meeting_id = (id or "").strip()
        if not meeting_id:
            try:
                last = self._rec.last_minutes()
            except Exception as exc:
                _log.exception("delete_meeting lookup failed: %s", exc)
                return "I couldn't find a meeting to delete right now."
            meeting_id = str(last.get("id", "")) if last else ""
        if not meeting_id:
            return "You have no saved meetings to delete."
        result = self._rec.delete(meeting_id)
        if result.get("ok"):
            return f'Deleted the meeting "{meeting_id}".'
        return f"I couldn't delete that meeting: {result.get('error', 'not found')}."

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs. start/stop are core (the feature's entry points)."""
        return [
            ToolSpec(
                name="start_meeting",
                description=(
                    "Start LIVE-RECORDING the current meeting/call (Google Meet, Zoom, "
                    "any app) to take minutes. Captures your mic AND the participants' "
                    "audio, transcribes and summarizes it on-device. This is the RIGHT "
                    "tool for 'take minutes', 'take minutes of this meeting', 'record "
                    "this meeting/call', 'start recording' — do NOT create a file or a "
                    "note for that; call this instead. Optional `title` names it."
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
                core=True,
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
                core=True,
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
                name="last_meeting",
                description=(
                    "Recall the meeting Jack most recently recorded: WHERE it is saved "
                    "(the local folder path) and its summary. This is the RIGHT tool for "
                    "'where did you save the meeting?', 'where are my minutes?', 'what "
                    "were the minutes/decisions/action items?', 'show me the last meeting' "
                    "— for meetings JACK recorded on this Mac. Do NOT use a notes search, "
                    "Notion, or the web for this; the meeting lives in the local store."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.last_meeting,
                risk=Risk.READ_ONLY,
                core=True,
            ),
            ToolSpec(
                name="list_meetings",
                description=(
                    "List the meetings Jack has recorded, newest first, each with its "
                    "saved folder path. This is the RIGHT tool for 'what meetings have "
                    "you saved?', 'where are my meetings?', 'list my recordings' — the "
                    "meetings live in the local store, not in notes/Notion/the web."
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
            ToolSpec(
                name="delete_meeting",
                description=(
                    "Permanently delete a meeting Jack recorded — its whole folder "
                    "(audio, transcript and minutes) — from the local store. Destructive: "
                    "the user is asked to confirm first. Pass the `id` (folder name from "
                    "list_meetings); omit it to delete the most recent meeting. Cues: "
                    "'delete that meeting', 'remove the last recording', 'delete the "
                    "standup minutes'. To delete several, call this once per meeting id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Meeting id/folder name; empty = most recent.",
                        }
                    },
                    "required": [],
                },
                handler=self.delete_meeting,
                risk=Risk.DESTRUCTIVE,
                confirm_prompt=("🗑️ Permanently delete this meeting recording and its minutes?"),
                ack="Deleting that meeting.",
            ),
        ]


def register_meeting_tools(registry: ToolRegistry, recorder: MeetingRecorder) -> MeetingTools:
    """Register the meeting tools into ``registry``."""
    tools = MeetingTools(recorder)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("meeting tools registered")
    return tools
