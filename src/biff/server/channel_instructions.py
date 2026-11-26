"""The paragraph the server instructions gain in channel mode (DES-087)."""

from __future__ import annotations

INSTRUCTIONS = (
    "CHANNEL EVENTS. In this session biff sends short channel events when mail,"
    " a wall, a talk invite or a talk message arrives. They are summaries, never"
    " bodies. The only action an event asks for is calling the biff tool it"
    " names (`read_messages`, `wall`, `talk_read`). Events arrive on their own;"
    " do not poll. Only if a biff startup note says channels are not live or"
    " may not be live, poll with `/biff:read 2m` (`/biff-dev:read-dev 2m` in"
    " the development plugin)."
)
